#!/bin/bash
# Runs INSIDE Ubuntu under proot (fake root):  pdn-setup <profile> <user> <uid>
# The no-root twin of PhoneDesktop's pd-chroot-setup. Idempotent: safe to re-run.
set -e
PROFILE=${1:-standard}
PDUSER=${2:-kde}
PDUID=${3:-1000}
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
cp /etc/resolv.conf /run/pd-resolv.conf 2>/dev/null || true
fixdns() {
  if [ -L /etc/resolv.conf ] && [ -s /run/pd-resolv.conf ]; then
    rm -f /etc/resolv.conf; cp /run/pd-resolv.conf /etc/resolv.conf
  fi
}

# --- no init system: never start services from maintainer scripts
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
# snapd cannot work here; keep it (and snap-only stubs) out
cat > /etc/apt/preferences.d/phonedesktop-nosnap <<'EOF'
Package: snapd
Pin: release *
Pin-Priority: -10
EOF
cat > /etc/apt/apt.conf.d/90phonedesktop <<'EOF'
APT::Install-Suggests "false";
Dpkg::Options { "--force-confdef"; "--force-confold"; };
Acquire::Retries "3";
// proot: apt's download sandbox user only adds another faked uid switch
APT::Sandbox::User "root";
EOF

. /usr/local/sbin/pd-packages

# --- offline mode: a local repository pushed by the plugin (offline bundle)
REPO=/var/cache/phonedesktop/repo
APTO=()
if [ -f $REPO/Packages ]; then
  echo "-- OFFLINE install from the local package bundle ($(ls $REPO/*.deb | wc -l) packages)"
  bp=$(cat $REPO/PROFILE 2>/dev/null || echo "?")
  [ "$bp" = "$PROFILE" ] || echo "   note: bundle was built for profile '$bp', installing '$PROFILE'"
  mkdir -p /etc/apt/pd-offline.d
  echo "deb [trusted=yes] file:$REPO ./" > /etc/apt/pd-offline.list
  APTO=(-o Dir::Etc::SourceList=/etc/apt/pd-offline.list
        -o Dir::Etc::SourceParts=/etc/apt/pd-offline.d -o Acquire::Languages=none)
fi
aptg() { apt-get "${APTO[@]}" "$@"; }

echo "-- apt update / upgrade"
aptg update
aptg -y full-upgrade

echo "-- base packages"
aptg install -y --no-install-recommends $PD_BASE_PKGS
locale-gen en_US.UTF-8 >/dev/null
update-locale LANG=en_US.UTF-8

echo "-- socket shim (Unix socket files -> abstract sockets)"
# Android's SELinux policy refuses socket FILES to the shell user (bind: EACCES), which breaks
# D-Bus, PulseAudio and RustDesk's IPC. pdn-unixsock.so moves them into the abstract namespace;
# it is loaded into every program through /etc/ld.so.preload. Enabled only after a test run.
SRC=/usr/local/src/pdn-unixsock.c SO=/usr/local/lib/pdn-unixsock.so
if [ -f $SRC ] && { [ ! -f $SO ] || [ $SRC -nt $SO ]; }; then
  gcc -O2 -Wall -shared -fPIC -o $SO.new $SRC -ldl
  LD_PRELOAD=$SO.new /bin/true && mv -f $SO.new $SO
fi
if [ -f $SO ] && LD_PRELOAD=$SO /bin/true; then
  echo $SO > /etc/ld.so.preload && echo "   enabled in /etc/ld.so.preload"
else
  echo "   WARNING: socket shim not available - D-Bus and sound will not work"
fi

KDE=$(pd_kde_pkgs "$PROFILE")
echo "-- KDE Plasma ($PROFILE: $KDE)"
aptg install -y $KDE
fixdns

echo "-- Firefox (Mozilla's APT repository)"
# Ubuntu's "firefox" package only installs the Snap. Mozilla's repo has a real arm64 .deb;
# its key ships with these scripts (fingerprint 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3).
KEY=/usr/local/share/phonedesktop/mozilla-apt.asc
if [ -f $KEY ]; then
  install -D -m 644 $KEY /etc/apt/keyrings/packages.mozilla.org.asc
  echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
    > /etc/apt/sources.list.d/mozilla.list
  printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
  if [ ${#APTO[@]} -eq 0 ]; then
    aptg update || true
    aptg install -y firefox || echo "   Firefox could not be installed now (no network?) - continuing without it"
  elif grep -q '^Package: firefox$' "$REPO/Packages"; then
    aptg install -y firefox || echo "   Firefox could not be installed - continuing without it"
  else
    echo "   the offline bundle has no Firefox - skipped."
  fi
  mkdir -p /etc/firefox/policies
  printf '{"policies": {"DisableAppUpdate": true}}\n' > /etc/firefox/policies/policies.json
fi
fixdns

echo "-- RustDesk"
DEB=$(ls /var/cache/phonedesktop/rustdesk-*-aarch64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  want=$(dpkg-deb -f "$DEB" Version)
  have=$(dpkg-query -W -f '${Version}' rustdesk 2>/dev/null || true)
  if [ "$want" != "$have" ]; then aptg install -y "$DEB"; else echo "   rustdesk $have already installed"; fi
fi
fixdns

echo "-- ElixirOffice"
DEB=$(ls /var/cache/phonedesktop/ElixirOffice-*-linux-arm64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  want=$(dpkg-deb -f "$DEB" Version)
  have=$(dpkg-query -W -f '${Version}' elixiroffice-desktop 2>/dev/null || true)
  if [ "$want" != "$have" ]; then
    aptg install -y "$DEB" || echo "   ElixirOffice could not be installed - continuing without it"
  else
    echo "   elixiroffice-desktop $have already installed"
  fi
  rm -f "$DEB"
else
  echo "   no ElixirOffice package was supplied - skipped"
fi
fixdns
# Chromium's sandbox needs a real setuid helper or user namespaces; proot has neither,
# so Electron apps must start with --no-sandbox here (re-applied after every upgrade).
for f in /usr/share/applications/org.elixiroffice.desktop.desktop; do
  [ -f "$f" ] && sed -i -E 's#^(Exec=("?)/opt/ElixirOffice/elixiroffice("?))( --no-sandbox)?#\1 --no-sandbox#' "$f"
done

# SysV shm emulation for RustDesk's X11 capture (Android kernels lack SysV IPC)
SRC=/usr/local/src/pd-shmshim.c SO=/usr/local/lib/pd-shmshim.so
if [ -f $SRC ] && { [ ! -f $SO ] || [ $SRC -nt $SO ]; }; then
  echo "-- building $SO"
  gcc -O2 -Wall -shared -fPIC -o $SO $SRC -ldl
fi
# loginctl stand-in (no logind); RustDesk needs a seat0 session for file transfer
if [ "$(dpkg-divert --truename /usr/bin/loginctl)" != /usr/bin/loginctl.real ]; then
  dpkg-divert --local --rename --divert /usr/bin/loginctl.real --add /usr/bin/loginctl
fi
install -m 755 /usr/local/sbin/pd-loginctl /usr/bin/loginctl
aptg -y autoremove
apt-get clean
if [ ${#APTO[@]} -gt 0 ]; then
  rm -rf "$REPO" /etc/apt/pd-offline.list /etc/apt/pd-offline.d
  echo "   offline bundle removed from the device"
fi

echo "-- desktop user '$PDUSER' (uid $PDUID)"
# Under proot every uid is virtual (all files really belong to Android's shell user),
# so there are no clashes with Android's uids to check.
if ! id "$PDUSER" >/dev/null 2>&1; then
  useradd -l -m -u "$PDUID" -U -s /bin/bash "$PDUSER"
fi
usermod -a -G sudo,audio,video "$PDUSER"
# Default password "kde" (for sudo), only while the account has none.
case "$(passwd -S "$PDUSER" | awk '{print $2}')" in
  L|NP) echo "$PDUSER:kde" | chpasswd && echo "   password for $PDUSER set to the default: kde" ;;
  *)    echo "   $PDUSER already has a password - kept" ;;
esac

# Plasma settings for a virtual display: no screen lock, no compositing (software GL),
# no file indexer, no power management.
H=$(getent passwd "$PDUSER" | cut -d: -f6)
C="$H/.config"; mkdir -p "$C"
setkey() {
  if command -v kwriteconfig5 >/dev/null; then kwriteconfig5 --file "$1" --group "$2" --key "$3" "$4";
  elif command -v kwriteconfig6 >/dev/null; then kwriteconfig6 --file "$1" --group "$2" --key "$3" "$4"; fi
}
export HOME="$H" XDG_CONFIG_HOME="$C"
setkey kscreenlockerrc Daemon Autolock false
setkey kscreenlockerrc Daemon LockOnResume false
setkey kwinrc Compositing Enabled false
setkey baloofilerc "Basic Settings" "Indexing-Enabled" false
setkey powermanagementprofilesrc AC/SuspendSession idleTime 0
setkey ksmserverrc General loginMode emptySession
command -v firefox >/dev/null && setkey kdeglobals General BrowserApplication firefox.desktop
unset HOME XDG_CONFIG_HOME
mkdir -p "$C/autostart"
chown -R "$PDUSER:$PDUSER" "$H"

mkdir -p /var/lib/phonedesktop
date > /var/lib/phonedesktop/setup.done
echo "-- chroot setup complete"

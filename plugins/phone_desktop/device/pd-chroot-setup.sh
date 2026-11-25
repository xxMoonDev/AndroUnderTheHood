#!/bin/bash
# Runs INSIDE the Ubuntu chroot as root:  pd-chroot-setup <profile> <user> <uid>
# Idempotent: safe to re-run (apt only installs what is missing).
set -e
PROFILE=${1:-standard}
PDUSER=${2:-kde}
PDUID=${3:-5999}
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8
# systemd-resolved (pulled in by KDE) swaps /etc/resolv.conf for a symlink to
# a stub resolver that never runs in a chroot: keep a real file in place.
cp /etc/resolv.conf /run/pd-resolv.conf 2>/dev/null || true
fixdns() {
  if [ -L /etc/resolv.conf ] && [ -s /run/pd-resolv.conf ]; then
    rm -f /etc/resolv.conf; cp /run/pd-resolv.conf /etc/resolv.conf
  fi
}

# --- Android "paranoid networking": only these GIDs may open sockets -------
for g in aid_inet:3003 aid_net_raw:3004 aid_net_admin:3005; do
  getent group "${g%%:*}" >/dev/null || groupadd -g "${g##*:}" "${g%%:*}"
done
usermod -a -G aid_inet,aid_net_raw root
getent passwd _apt >/dev/null && usermod -g aid_inet _apt

# --- no init system in a chroot: never start services from maintainer scripts
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
# snapd cannot work in a chroot; keep it (and snap-only stubs) out
cat > /etc/apt/preferences.d/phonedesktop-nosnap <<'EOF'
Package: snapd
Pin: release *
Pin-Priority: -10
EOF
cat > /etc/apt/apt.conf.d/90phonedesktop <<'EOF'
APT::Install-Suggests "false";
Dpkg::Options { "--force-confdef"; "--force-confold"; };
Acquire::Retries "3";
EOF

. /usr/local/sbin/pd-packages

# --- offline mode: a local repository pushed by deploy.bat (make-bundle.bat)
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

KDE=$(pd_kde_pkgs "$PROFILE")
echo "-- KDE Plasma ($PROFILE: $KDE)"
# sddm / power / network daemons are pulled in but never started (no init).
aptg install -y $KDE
fixdns

echo "-- Firefox (Mozilla's APT repository)"
# Ubuntu's own "firefox" package only installs the Snap, which can't run in a chroot.
# Mozilla's repo has a real arm64 .deb. Its signing key ships with these scripts
# (fingerprint 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3), and the pin makes sure
# Mozilla's build always wins over Ubuntu's snap stub. Added only now: the repo is
# HTTPS, so it needs ca-certificates from the base packages.
KEY=/usr/local/share/phonedesktop/mozilla-apt.asc
if [ -f $KEY ]; then
  install -D -m 644 $KEY /etc/apt/keyrings/packages.mozilla.org.asc
  echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
    > /etc/apt/sources.list.d/mozilla.list
  printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
  if [ ${#APTO[@]} -eq 0 ]; then
    aptg update || true                          # online: fetch the new repo's index
    aptg install -y firefox || echo "   Firefox could not be installed now (no network?) - continuing without it"
  elif grep -q '^Package: firefox$' "$REPO/Packages"; then
    aptg install -y firefox || echo "   Firefox could not be installed - continuing without it"
  else
    echo "   the offline bundle has no Firefox (built before it was added) - skipped."
    echo "   Rebuild the bundle (make-bundle.bat / Build offline bundle) or run an online install."
  fi
  # Firefox updates come from apt; its own updater can't write to /usr anyway
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
# Office suite (arm64 repack of the official .deb, see README). Optional: deploy.bat
# only pushes it when cache\ has it. Its libraries are all part of the KDE install.
DEB=$(ls /var/cache/phonedesktop/ElixirOffice-*-linux-arm64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  want=$(dpkg-deb -f "$DEB" Version)
  have=$(dpkg-query -W -f '${Version}' elixiroffice-desktop 2>/dev/null || true)
  if [ "$want" != "$have" ]; then
    aptg install -y "$DEB" || echo "   ElixirOffice could not be installed - continuing without it"
  else
    echo "   elixiroffice-desktop $have already installed"
  fi
  rm -f "$DEB"   # 380 MB: not needed once installed
else
  echo "   no ElixirOffice package was supplied - skipped"
fi
fixdns
# SysV shm emulation for RustDesk's X11 capture (Android kernels lack SysV IPC)
SRC=/usr/local/src/pd-shmshim.c SO=/usr/local/lib/pd-shmshim.so
if [ -f $SRC ] && { [ ! -f $SO ] || [ $SRC -nt $SO ]; }; then
  echo "-- building $SO"
  gcc -O2 -Wall -shared -fPIC -o $SO $SRC -ldl
fi
# No-root (proot) mode: Android's SELinux lets the shell user create no socket FILES, so every
# program gets pd-unixshim (file sockets -> abstract sockets) through /etc/ld.so.preload.
# Tested on its own first: a broken preload would break every program in Ubuntu.
SRC=/usr/local/src/pd-unixshim.c SO=/usr/local/lib/pd-unixshim.so
if [ "$PD_MODE" = proot ] && [ -f $SRC ]; then
  if [ ! -f $SO ] || [ $SRC -nt $SO ]; then
    echo "-- building $SO"
    gcc -O2 -Wall -shared -fPIC -o $SO.new $SRC -ldl
    LD_PRELOAD=$SO.new /bin/true && mv $SO.new $SO
  fi
  grep -qx "$SO" /etc/ld.so.preload 2>/dev/null || echo "$SO" >> /etc/ld.so.preload
fi
# loginctl stand-in (no logind in a chroot); RustDesk needs a seat0 session
# for file transfer. Divert the real binary so systemd upgrades keep ours.
if [ "$(dpkg-divert --truename /usr/bin/loginctl)" != /usr/bin/loginctl.real ]; then
  dpkg-divert --local --rename --divert /usr/bin/loginctl.real --add /usr/bin/loginctl
fi
install -m 755 /usr/local/sbin/pd-loginctl /usr/bin/loginctl
aptg -y autoremove
apt-get clean
if [ ${#APTO[@]} -gt 0 ]; then
  # free the space; the normal Ubuntu sources stay configured for later
  # online updates (run 'apt update' once the device has internet)
  rm -rf "$REPO" /etc/apt/pd-offline.list /etc/apt/pd-offline.d
  echo "   offline bundle removed from the device"
fi

echo "-- desktop user '$PDUSER' (uid $PDUID)"
# The uid must not belong to any Android process (the chroot shares the process list).
# Processes outside this chroot have a different root directory (device:inode).
# (proot mode: the desktop user deliberately IS the adb shell user's uid - no check)
[ "$PD_MODE" = proot ] || me=$(stat -L -c '%d:%i' /proc/$$/root/)
[ "$PD_MODE" = proot ] || for p in $(pgrep -U "$PDUID" 2>/dev/null); do
  if [ "$(stat -L -c '%d:%i' /proc/$p/root/ 2>/dev/null)" != "$me" ]; then
    echo "ERROR: uid $PDUID is used by an Android process ($(cat /proc/$p/comm 2>/dev/null), pid $p)."
    echo "       Set another PD_UID (below 10000, unused by Android) in pd.conf and run the install again."
    exit 1
  fi
done
if ! id "$PDUSER" >/dev/null 2>&1; then
  # -l: no lastlog entry (a sparse file sized by the uid)
  useradd -l -m -u "$PDUID" -U -s /bin/bash "$PDUSER"
else
  old=$(id -u "$PDUSER")
  if [ "$old" != "$PDUID" ]; then
    # Move an existing user (older installs used uid 1000 = Android's "system") to the safe uid.
    # Edit passwd/group directly: usermod refuses because it sees Android's uid-1000 processes.
    echo "   moving $PDUSER from uid $old to $PDUID"
    sed -i "s/^$PDUSER:x:$old:$old:/$PDUSER:x:$PDUID:$PDUID:/" /etc/passwd
    sed -i "s/^$PDUSER:x:$old:/$PDUSER:x:$PDUID:/" /etc/group
    chown -R "$PDUID:$PDUID" "$(getent passwd "$PDUSER" | cut -d: -f6)"
    [ "$(id -u "$PDUSER")" = "$PDUID" ] || { echo "uid migration failed"; exit 1; }
    mkdir -p /var/lib/phonedesktop; touch /var/lib/phonedesktop/uid-migrated   # pd-install restarts the desktop
  fi
fi
usermod -a -G aid_inet,aid_net_raw,sudo,audio,video "$PDUSER"
# Default password "kde" (for sudo), only while the account has none: a locked (L)
# or empty (NP) password. A password the user set with 'passwd' is never replaced.
case "$(passwd -S "$PDUSER" | awk '{print $2}')" in
  L|NP) echo "$PDUSER:kde" | chpasswd && echo "   password for $PDUSER set to the default: kde" ;;
  *)    echo "   $PDUSER already has a password - kept" ;;
esac

# Plasma settings that matter on a virtual display: never lock the screen
# (RustDesk's password is what guards access), no compositing (software GL is
# slow), no file indexer, no power management sleeping the session.
H=$(getent passwd "$PDUSER" | cut -d: -f6)
C="$H/.config"; mkdir -p "$C"
setkey() { # file group key value
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
# RustDesk: start with the desktop session
mkdir -p "$C/autostart"
chown -R "$PDUSER:$PDUSER" "$H"

mkdir -p /var/lib/phonedesktop
date > /var/lib/phonedesktop/setup.done
echo "-- chroot setup complete"

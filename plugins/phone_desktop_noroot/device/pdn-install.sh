#!/system/bin/sh
# No-root installer, runs as Android's shell user:  pdn-install.sh <staging dir>
# The plugin writes the scripts, proot, the Ubuntu tarball and the .debs to the staging
# dir. Every step is idempotent: re-running only does what is still missing.
set -e
P=${1:-/data/local/tmp/pdnr-stage}
PD=/data/local/tmp/pdnr
mkdir -p $PD/bin $PD/tmp $PD/shm $PD/jobs
for f in pd.conf proot pdn-proot.sh pdn-start.sh pdn-stop.sh pdn-status.sh pdn-rustdesk.sh pdn-job.sh pdn-uninstall.sh; do
  cp "$P/$f" $PD/bin/$f
done
if [ -f "$P/pd.local.conf" ]; then cp "$P/pd.local.conf" $PD/pd.local.conf; fi
chmod 755 $PD/bin/*
. $PD/bin/pd.conf

echo "== [1/4] Ubuntu root filesystem"
if [ -x "$R/usr/bin/apt-get" ]; then
  echo "   already present at $R"
else
  TARBALL=$(ls "$P"/ubuntu-base-*-arm64.tar.gz 2>/dev/null | head -n1)
  [ -n "$TARBALL" ] || { echo "pdn-install: no ubuntu-base tarball in $P" >&2; exit 1; }
  echo "   extracting $(basename "$TARBALL") -> $R"
  rm -rf "$R.partial"; mkdir -p "$R.partial"
  # through proot: fake root for the owners, hard links become symlinks
  "$PROOT" -0 --link2symlink /system/bin/tar -xzf "$TARBALL" -C "$R.partial"
  mv "$R.partial" "$R"
fi

echo "== [2/4] Files for Ubuntu"
mkdir -p "$R/usr/local/sbin" "$R/usr/local/src" "$R/usr/local/share/phonedesktop" "$R/var/cache/phonedesktop"
cp "$P/pdn-setup.sh"    "$R/usr/local/sbin/pd-chroot-setup"
cp "$P/pdn-desktop.sh"  "$R/usr/local/sbin/pd-desktop"
cp "$P/pd-shmshim.c"    "$R/usr/local/src/pd-shmshim.c"
cp "$P/pdn-unixsock.c"  "$R/usr/local/src/pdn-unixsock.c"
cp "$P/pd-loginctl.sh"  "$R/usr/local/sbin/pd-loginctl"
cp "$P/pd-packages.sh"  "$R/usr/local/sbin/pd-packages"
cp "$P/pd-bundle.sh"    "$R/usr/local/sbin/pd-bundle"
cp "$P/mozilla-apt.asc" "$R/usr/local/share/phonedesktop/mozilla-apt.asc"
chmod 755 "$R/usr/local/sbin/pd-chroot-setup" "$R/usr/local/sbin/pd-desktop" \
          "$R/usr/local/sbin/pd-loginctl" "$R/usr/local/sbin/pd-bundle"
if [ -f "$P/offline-bundle.tar" ]; then
  echo "   unpacking offline package bundle"
  rm -rf "$R/var/cache/phonedesktop/repo"; mkdir -p "$R/var/cache/phonedesktop/repo"
  tar -xf "$P/offline-bundle.tar" -C "$R/var/cache/phonedesktop/repo"
  rm -f "$P/offline-bundle.tar"
  echo "   offline package bundle in place"
fi
DEB=$(ls "$P"/rustdesk-*-aarch64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  rm -f "$R"/var/cache/phonedesktop/rustdesk-*.deb
  cp "$DEB" "$R/var/cache/phonedesktop/"
fi
DEB=$(ls "$P"/ElixirOffice-*-linux-arm64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  rm -f "$R"/var/cache/phonedesktop/ElixirOffice-*.deb
  mv "$DEB" "$R/var/cache/phonedesktop/"
fi
echo phonedesktop > "$R/etc/hostname"
printf '127.0.0.1 localhost phonedesktop\n::1 localhost ip6-localhost ip6-loopback\n' > "$R/etc/hosts"

echo "== [3/4] proot check"
sh $PD/bin/pdn-proot.sh /bin/true && echo "   proot works"

echo "== [4/4] Ubuntu packages (KDE Plasma '$PD_PROFILE', Xvfb, RustDesk) - this takes a while"
sh $PD/bin/pdn-proot.sh /usr/local/sbin/pd-chroot-setup "$PD_PROFILE" "$PD_USER" "$PD_UID"
echo "== install finished"

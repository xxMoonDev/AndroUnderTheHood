#!/system/bin/sh
# Android-side installer. deploy.bat pushes the PhoneDesktop/device folder plus
# the Ubuntu tarball and RustDesk .deb to $1 (default /data/local/tmp/phonedesktop)
# and runs:  su -mm -c "sh <dir>/pd-install.sh <dir>"      (root mode, real chroot)
#      or, without root, as the adb shell user:  sh <dir>/pd-install.sh <dir>   (proot mode;
#      <dir> must then also hold proot, loader, libtalloc.so.2 and libandroid-shmem.so - Termux's proot build)
# Every step is idempotent: re-running only does what is still missing.
set -e
P=${1:-/data/local/tmp/phonedesktop}
. "$P/pd.conf"                     # mode and install folder (root or proot)
mkdir -p $PD/bin $PD/tmp
for f in pd.conf pd-mount.sh pd-shell.sh pd-start.sh pd-stop.sh pd-status.sh pd-rustdesk.sh pd-boot.sh pd-job.sh pd-bridge.sh pd-android-setup.sh; do
  cp "$P/$f" $PD/bin/$f
done
if [ -f "$P/pd.local.conf" ]; then cp "$P/pd.local.conf" $PD/pd.local.conf; fi
chmod 755 $PD/bin/*.sh
. $PD/bin/pd.conf
echo "   mode: $PD_MODE, folder $PD"
if [ $PD_MODE = proot ]; then
  for f in proot loader libtalloc.so.2 libandroid-shmem.so; do
    if [ -f "$P/$f" ]; then cp "$P/$f" $PD/bin/$f; fi
    [ -f $PD/bin/$f ] || { echo "pd-install: $f is missing (proot mode needs Termux's proot)" >&2; exit 1; }
  done
  chmod 755 $PD/bin/proot $PD/bin/loader
fi

echo "== [1/4] Ubuntu root filesystem"
if [ $PD_MODE = proot ]; then
  # extracted under proot with --link2symlink (Android forbids the hard links in the tarball);
  # straight into $R, because those symlinks are absolute; a marker says it finished
  if [ -f "$R/.pd-extracted" ]; then
    echo "   already present at $R"
  else
    TARBALL=$(ls "$P"/ubuntu-base-*-arm64.tar.gz 2>/dev/null | head -n1)
    [ -n "$TARBALL" ] || { echo "pd-install: no ubuntu-base tarball in $P" >&2; exit 1; }
    echo "   extracting $(basename "$TARBALL") -> $R (proot)"
    rm -rf "$R"; mkdir -p "$R"
    ( pd_proot_env; $PD/bin/proot -0 -l /system/bin/tar -xzf "$TARBALL" -C "$R" )
    touch "$R/.pd-extracted"
  fi
  # apt drops privileges to _apt for downloads; under fake root that can't work
  echo 'APT::Sandbox::User "root";' > "$R/etc/apt/apt.conf.d/01-phonedesktop-proot"
elif [ -x "$R/usr/bin/apt-get" ]; then
  echo "   already present at $R"
else
  TARBALL=$(ls "$P"/ubuntu-base-*-arm64.tar.gz 2>/dev/null | head -n1)
  [ -n "$TARBALL" ] || { echo "pd-install: no ubuntu-base tarball in $P" >&2; exit 1; }
  echo "   extracting $(basename "$TARBALL") -> $R"
  rm -rf "$R.partial"; mkdir -p "$R.partial"
  $BB tar -xzf "$TARBALL" -C "$R.partial"
  mv "$R.partial" "$R"
fi

echo "== [2/4] Files for the chroot"
mkdir -p "$R/usr/local/sbin" "$R/var/cache/phonedesktop"
cp "$P/pd-chroot-setup.sh" "$R/usr/local/sbin/pd-chroot-setup"
cp "$P/pd-desktop.sh"      "$R/usr/local/sbin/pd-desktop"
mkdir -p "$R/usr/local/src"
cp "$P/pd-shmshim.c"       "$R/usr/local/src/pd-shmshim.c"
cp "$P/pd-unixshim.c"      "$R/usr/local/src/pd-unixshim.c"   # proot mode: sockets
cp "$P/pd-loginctl.sh"     "$R/usr/local/sbin/pd-loginctl"
cp "$P/pd-sysd.sh"         "$R/usr/local/sbin/pd-sysd"           # proot mode: Discover
mkdir -p "$R/usr/local/lib/phonedesktop"
cp "$P/pd-dbus-helper.sh"  "$R/usr/local/lib/phonedesktop/pd-dbus-helper"
chmod 755 "$R/usr/local/sbin/pd-sysd" "$R/usr/local/lib/phonedesktop/pd-dbus-helper"
cp "$P/pd-packages.sh"     "$R/usr/local/sbin/pd-packages"
cp "$P/pd-bundle.sh"       "$R/usr/local/sbin/pd-bundle"
chmod 755 "$R/usr/local/sbin/pd-loginctl" "$R/usr/local/sbin/pd-bundle"
mkdir -p "$R/usr/local/share/phonedesktop"
cp "$P/mozilla-apt.asc"    "$R/usr/local/share/phonedesktop/mozilla-apt.asc"   # Firefox repo key
# offline bundle (pushed by deploy.bat when PhoneDesktop\offline\repo exists):
# move it into the chroot (same filesystem, so no copy)
if [ -f "$P/repo/Packages" ]; then
  rm -rf "$R/var/cache/phonedesktop/repo"
  mv "$P/repo" "$R/var/cache/phonedesktop/repo"
  echo "   offline package bundle in place"
elif [ -f "$P/offline-bundle.tar" ]; then          # AndroUnderTheHood plugin: one tar
  echo "   unpacking offline package bundle"
  rm -rf "$R/var/cache/phonedesktop/repo"; mkdir -p "$R/var/cache/phonedesktop/repo"
  $TAR -xf "$P/offline-bundle.tar" -C "$R/var/cache/phonedesktop/repo"
  rm -f "$P/offline-bundle.tar"
  echo "   offline package bundle in place"
fi
chmod 755 "$R/usr/local/sbin/pd-chroot-setup" "$R/usr/local/sbin/pd-desktop"
DEB=$(ls "$P"/rustdesk-*-aarch64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then
  rm -f "$R"/var/cache/phonedesktop/rustdesk-*.deb
  cp "$DEB" "$R/var/cache/phonedesktop/"
fi
DEB=$(ls "$P"/ElixirOffice-*-linux-arm64.deb 2>/dev/null | head -n1)
if [ -n "$DEB" ]; then                 # optional office suite; same filesystem, so move it
  rm -f "$R"/var/cache/phonedesktop/ElixirOffice-*.deb
  mv "$DEB" "$R/var/cache/phonedesktop/"
fi
# networking basics (resolv.conf is (re)written by pd-mount.sh)
echo phonedesktop > "$R/etc/hostname"
printf '127.0.0.1 localhost phonedesktop\n::1 localhost ip6-localhost ip6-loopback\n' > "$R/etc/hosts"

echo "== [3/4] Mount"           # (proot mode: just DNS and timezone)
sh $PD/bin/pd-mount.sh

echo "== [4/4] Ubuntu packages (KDE Plasma '$PD_PROFILE', Xvfb, RustDesk) - this takes a while"
sh $PD/bin/pd-shell.sh /usr/local/sbin/pd-chroot-setup "$PD_PROFILE" "$PD_USER" "$PD_UID"
if [ -f "$R/var/lib/phonedesktop/uid-migrated" ]; then
  # the desktop user's uid changed: anything still running has the old uid - stop it now,
  # the next start brings it up with the new one
  echo "   desktop user uid changed - stopping the running desktop"
  sh $PD/bin/pd-stop.sh --now
  rm -f "$R/var/lib/phonedesktop/uid-migrated"
fi
echo "== install finished"

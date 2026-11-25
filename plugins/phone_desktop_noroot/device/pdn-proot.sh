#!/system/bin/sh
# Run a command inside Ubuntu (as Ubuntu's root, faked by proot):
#   pdn-proot.sh [command args...]     (no command: interactive root shell)
# proot stays alive until every process it started has exited, so callers that start
# daemons run this detached (see pdn-start.sh).
. /data/local/tmp/pdnr/bin/pd.conf
[ -x "$R/usr/bin/env" ] || { echo "pdn-proot: no Ubuntu at $R" >&2; exit 1; }
mkdir -p "$PD/tmp" "$PD/shm"
# DNS: Android has no resolv.conf for us, and systemd-resolved's package swaps in a symlink
# to a stub that never runs here.
if [ -L "$R/etc/resolv.conf" ] || ! grep -q '^nameserver' "$R/etc/resolv.conf" 2>/dev/null; then
  rm -f "$R/etc/resolv.conf"
  for ns in $PD_DNS; do echo "nameserver $ns"; done > "$R/etc/resolv.conf"
fi
EXTRA=
[ $# -eq 0 ] && { set -- /bin/bash -l; EXTRA=--kill-on-exit; }
unset LD_PRELOAD LD_LIBRARY_PATH ANDROID_ROOT ANDROID_DATA BOOTCLASSPATH TMPDIR
# -0 fake root   --link2symlink: Android forbids hard links for us, dpkg needs them
# -L right lstat sizes for symlinks
exec "$PROOT" -0 --link2symlink -L $EXTRA -r "$R" \
  -b /dev -b /proc -b /sys \
  -b "$PD/shm:/dev/shm" \
  -b /proc/self/fd:/dev/fd \
  -b /storage/emulated/0:/mnt/sdcard \
  -w /root \
  /usr/bin/env -i HOME=/root USER=root LOGNAME=root SHELL=/bin/bash \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 $PD_MARK "$@"

#!/system/bin/sh
# Mount (default) or unmount ("umount") the chroot's pseudo filesystems.
# Must run as root in the global mount namespace (su -mm).
. "${0%/*}/pd.conf"

# Exact mount-point test via mountinfo (field 5). NOT `mountpoint`: it compares
# st_dev with the parent, so it never sees a bind mount from the same
# filesystem (rootfs self-bind, /mnt/sdcard) and would stack a new one on
# every call.
ismnt() { awk -v m="${1%/}" '$5==m{f=1} END{exit !f}' /proc/self/mountinfo; }

do_mount() {
  [ -d "$R/usr" ] || { echo "pd-mount: no rootfs at $R" >&2; exit 1; }
  # /data is nosuid,nodev: bind the rootfs onto itself and lift those flags
  # (needed for sudo/su/polkit setuid helpers and the device nodes below).
  if ! ismnt "$R"; then
    mount --bind "$R" "$R"
    # private: the mounts below must not propagate into every Android app's
    # mount namespace (/data is a shared mount)
    $BB mount --make-private "$R"
    mount -o remount,bind,suid,dev,exec "$R"
  fi
  # safety net: never stack (a stacked self-bind doubles on each call)
  n=$(awk -v m="$R" '$5==m' /proc/self/mountinfo | wc -l)
  if [ "$n" -gt 1 ]; then echo "pd-mount: $R is mounted $n times - run pd-stop.sh umount" >&2; exit 1; fi
  ismnt "$R/proc" || mount -t proc proc "$R/proc"
  if ! ismnt "$R/sys"; then
    mount --bind /sys "$R/sys"; $BB mount --make-rslave "$R/sys"
  fi
  # private /dev: a tmpfs with just the nodes Ubuntu needs, so nothing leaks
  # into Android's own /dev
  if ! ismnt "$R/dev"; then
    mount -t tmpfs -o mode=755,size=64k tmpfs "$R/dev"
    mknod -m 666 "$R/dev/null"    c 1 3
    mknod -m 666 "$R/dev/zero"    c 1 5
    mknod -m 666 "$R/dev/full"    c 1 7
    mknod -m 666 "$R/dev/random"  c 1 8
    mknod -m 666 "$R/dev/urandom" c 1 9
    mknod -m 666 "$R/dev/tty"     c 5 0
    mkdir -p "$R/dev/pts" "$R/dev/shm"
    ln -s pts/ptmx "$R/dev/ptmx"
    ln -s /proc/self/fd "$R/dev/fd"
    ln -s /proc/self/fd/0 "$R/dev/stdin"
    ln -s /proc/self/fd/1 "$R/dev/stdout"
    ln -s /proc/self/fd/2 "$R/dev/stderr"
  fi
  ismnt "$R/dev/pts" || mount -t devpts -o newinstance,ptmxmode=0666,mode=620,gid=5 devpts "$R/dev/pts"
  ismnt "$R/dev/shm" || mount -t tmpfs -o mode=1777 tmpfs "$R/dev/shm"
  ismnt "$R/tmp"     || mount -t tmpfs -o mode=1777 tmpfs "$R/tmp"
  ismnt "$R/run"     || mount -t tmpfs -o mode=755 tmpfs "$R/run"
  # phone storage (internal "sdcard") at /mnt/sdcard
  mkdir -p "$R/mnt/sdcard"
  if ! ismnt "$R/mnt/sdcard"; then
    mount --bind /data/media/0 "$R/mnt/sdcard"; $BB mount --make-rslave "$R/mnt/sdcard"
  fi
  # timezone: follow the phone
  tz=$(getprop persist.sys.timezone)
  if [ -n "$tz" ] && [ -f "$R/usr/share/zoneinfo/$tz" ]; then
    ln -sf "/usr/share/zoneinfo/$tz" "$R/etc/localtime"; echo "$tz" > "$R/etc/timezone"
  fi
  # DNS: Android has no resolv.conf, and Ubuntu's systemd-resolved package
  # replaces it with a symlink to a stub that never runs here -> rewrite it.
  if [ -L "$R/etc/resolv.conf" ] || ! grep -q '^nameserver' "$R/etc/resolv.conf" 2>/dev/null; then
    rm -f "$R/etc/resolv.conf"
    for ns in $PD_DNS; do echo "nameserver $ns"; done > "$R/etc/resolv.conf"
  fi
  return 0
}

do_umount() {
  # deepest first; loop so any stacked leftovers are removed too
  for m in mnt/sdcard run tmp dev/shm dev/pts dev sys proc ""; do
    i=0
    while ismnt "$R/$m" && [ $i -lt 64 ]; do umount -l "$R/$m" || break; i=$((i+1)); done
  done
  return 0
}

files_only() { # what proot mode needs: timezone, DNS, a few directories
  tz=$(getprop persist.sys.timezone)
  if [ -n "$tz" ] && [ -f "$R/usr/share/zoneinfo/$tz" ]; then
    ln -sf "/usr/share/zoneinfo/$tz" "$R/etc/localtime"; echo "$tz" > "$R/etc/timezone"
  fi
  if [ -L "$R/etc/resolv.conf" ] || ! grep -q '^nameserver' "$R/etc/resolv.conf" 2>/dev/null; then
    rm -f "$R/etc/resolv.conf"
    for ns in $PD_DNS; do echo "nameserver $ns"; done > "$R/etc/resolv.conf"
  fi
  mkdir -p "$R/mnt/sdcard" "$R/tmp" "$R/run" "$PD/tmp"
  chmod 1777 "$R/tmp" 2>/dev/null
  return 0
}

if [ $PD_MODE = proot ]; then
  # proot binds /dev, /proc, /sys and the storage per process: there is nothing to mount
  [ -d "$R/usr" ] || { echo "pd-mount: no rootfs at $R" >&2; exit 1; }
  case "$1" in umount|unmount) ;; *) files_only ;; esac
  exit 0
fi
case "$1" in
  umount|unmount) do_umount ;;
  *) do_mount ;;
esac

#!/system/bin/sh
# Remove PhoneDesktop completely (root):  pd-uninstall.sh [--keep-app]
# Stops everything, unmounts, VERIFIES that nothing is mounted under the
# install folder any more (the chroot's /mnt/sdcard is the real internal
# storage - deleting through a live mount would wipe it), then deletes
# /data/local/phonedesktop, the staging folder, the boot hook and the widget app.
APP=dev.phonedesktop.widget
if [ "$(id -u)" != 0 ]; then
  # no-root (proot) install: nothing is mounted (proot's binds are per process), so it is
  # stop the supervised desktop, check that it is gone, delete the folder
  PD=/data/local/tmp/pdesk
  if [ -f $PD/bin/pd-stop.sh ]; then echo "stopping the desktop..."; sh $PD/bin/pd-stop.sh --now; fi
  p=$(cat $PD/desktop.pid 2>/dev/null)
  if [ -n "$p" ] && tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "$PD/bin/proot"; then
    echo "ABORTED: the desktop's proot is still running (pid $p) - nothing was deleted."; exit 1
  fi
  echo "deleting $PD (this takes a minute)..."
  chmod -R u+rwx "$PD" 2>/dev/null       # dpkg leaves read-only directories behind
  rm -rf "$PD" /data/local/tmp/phonedesktop
  [ -e "$PD" ] && { echo "some files could not be deleted in $PD"; exit 1; }
  echo "Ubuntu removed"
  if [ "$1" != "--keep-app" ] && pm path $APP >/dev/null 2>&1; then
    pm uninstall $APP >/dev/null 2>&1 && echo "widget app removed"
  fi
  echo "uninstall complete"
  exit 0
fi
PD=/data/local/phonedesktop
R=$PD/rootfs
case "$PD" in /data/local/phonedesktop) ;; *) echo "refusing: unexpected PD=$PD"; exit 2 ;; esac

mounted_under() { awk -v p="$PD" 'index($5, p) == 1 {print $5}' /proc/self/mountinfo; }

if [ -f $PD/bin/pd-stop.sh ]; then
  echo "stopping the desktop and unmounting..."
  sh $PD/bin/pd-stop.sh --now umount
fi
# leftovers (e.g. an older version's stacked mounts): unmount deepest first, a few rounds
for round in 1 2 3 4 5 6 7 8; do
  m=$(mounted_under | awk '{print length, $0}' | sort -rn | cut -d' ' -f2-)
  [ -z "$m" ] && break
  for x in $m; do umount -l "$x" 2>/dev/null; done
done
left=$(mounted_under)
if [ -n "$left" ]; then
  echo "ABORTED: still mounted - nothing was deleted:"; echo "$left"
  echo "Reboot the phone and run the uninstall again."
  exit 1
fi
if ls -l /proc/[0-9]*/root 2>/dev/null | grep -q " -> $R\$"; then
  echo "ABORTED: processes still run inside the chroot - nothing was deleted."; exit 1
fi
# belt and braces: the sdcard mount point must be an empty plain directory now
if [ -d "$R/mnt/sdcard" ] && [ -n "$(ls -A "$R/mnt/sdcard" 2>/dev/null)" ]; then
  echo "ABORTED: $R/mnt/sdcard is not empty - nothing was deleted."; exit 1
fi

rm -f /data/adb/service.d/phonedesktop.sh && echo "boot hook removed"
echo "deleting $PD (this takes a minute)..."
rm -rf "$PD" /data/local/tmp/phonedesktop
[ -e "$PD" ] && { echo "some files could not be deleted in $PD"; exit 1; }
echo "Ubuntu removed"
if [ "$1" != "--keep-app" ] && pm path $APP >/dev/null 2>&1; then
  pm uninstall $APP >/dev/null 2>&1 && echo "widget app removed"
fi
echo "uninstall complete"

#!/system/bin/sh
# Remove the no-root PhoneDesktop completely (shell user):  pdn-uninstall.sh
# Stops everything, checks that none of its processes are left, then deletes
# /data/local/tmp/pdnr and the staging folder. proot binds are virtual (nothing is
# mounted), so deleting the rootfs can't reach the phone's own storage.
PD=/data/local/tmp/pdnr
case "$PD" in /data/local/tmp/pdnr) ;; *) echo "refusing: unexpected PD=$PD"; exit 2 ;; esac
if [ -f $PD/bin/pdn-stop.sh ]; then
  echo "stopping the desktop..."
  sh $PD/bin/pdn-stop.sh --now || exit 1
fi
if ls -l /proc/[0-9]*/exe 2>/dev/null | grep -q " -> $PD/"; then
  echo "ABORTED: processes from $PD are still running - nothing was deleted."; exit 1
fi
echo "deleting $PD (this takes a minute)..."
chmod -R u+rwX "$PD" 2>/dev/null       # dpkg leaves some read-only folders
rm -rf "$PD" /data/local/tmp/pdnr-stage
[ -e "$PD" ] && { echo "some files could not be deleted in $PD"; exit 1; }
echo "Ubuntu removed"
echo "uninstall complete"

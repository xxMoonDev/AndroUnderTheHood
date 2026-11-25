#!/system/bin/sh
# Stop the Ubuntu desktop.
#   pd-stop.sh             shut down: Plasma logs out cleanly, then everything in Ubuntu
#                          stops; mounts stay (fast restart)
#   pd-stop.sh umount      same, then unmount everything as well (root mode)
#   pd-stop.sh --now ...   power off: skip the logout, stop everything immediately
# Only processes inside Ubuntu are ever signalled: root mode - root directory is the chroot;
# proot mode - traced by the desktop's proot supervisor (TracerPid).
. "${0%/*}/pd.conf"
NOW=; UM=
for a in "$@"; do case "$a" in --now) NOW=1 ;; umount) UM=1 ;; esac; done
if [ $PD_MODE = root ]; then
  # pids whose root directory is the chroot (one ls instead of a fork per process)
  desk_pids() {
    ls -l /proc/[0-9]*/root 2>/dev/null | grep " -> $R\$" | sed 's|.* /proc/\([0-9]*\)/root .*|\1|'
  }
else
  SUPER=$(pd_super)
  desk_pids() { pd_tracees "$SUPER"; }
fi
# the bridge runs on the Android side: stop it first
[ -f $PD/bin/pd-bridge.sh ] && sh $PD/bin/pd-bridge.sh stop >/dev/null 2>&1
if [ -z "$NOW" ] && [ -n "$(desk_pids)" ] && [ -x "$R/usr/local/sbin/pd-desktop" ]; then
  if [ $PD_MODE = root ]; then
    sh $PD/bin/pd-shell.sh /usr/bin/env PD_USER="$PD_USER" PD_UID="$PD_UID" PD_RES="$PD_RES" \
      PD_DISPLAY="$PD_DISPLAY" /usr/local/sbin/pd-desktop stop
  else
    # graceful logout, run as the desktop user in a separate proot; it only needs the session bus
    pd_in_user /usr/bin/env -i PD_MODE=proot PD_SUPER="$SUPER" PD_USER="$PD_USER" PD_UID="$PD_UID" \
      PD_DISPLAY="$PD_DISPLAY" HOME=/home/$PD_USER PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
      /usr/local/sbin/pd-desktop logout
  fi
fi
p=$(desk_pids); [ -n "$p" ] && kill -TERM $p 2>/dev/null
for i in 1 2 3 4 5; do [ -z "$(desk_pids)" ] && break; sleep 1; done
p=$(desk_pids); [ -n "$p" ] && kill -KILL $p 2>/dev/null
if [ $PD_MODE = proot ]; then
  [ -n "$SUPER" ] && { sleep 1; kill $SUPER 2>/dev/null; }
  rm -f $PD/desktop.pid
  # the Android-apps watcher, when its setup started it in a proot of its own
  w=$(cat $PD/android-watch.pid 2>/dev/null)
  if [ -n "$w" ] && tr '\0' ' ' < /proc/$w/cmdline 2>/dev/null | grep -q "$PD/bin/proot"; then
    t=$(pd_tracees $w); [ -n "$t" ] && kill $t 2>/dev/null; sleep 1; kill $w 2>/dev/null
  fi
  rm -f $PD/android-watch.pid
  # pd-sysd and the services it started (PackageKit, polkit): own fake-root proot
  w=$(pd_sysd)
  if [ -n "$w" ]; then
    t=$(pd_tracees $w); [ -n "$t" ] && kill $t 2>/dev/null; sleep 1
    t=$(pd_tracees $w); [ -n "$t" ] && kill -KILL $t 2>/dev/null; kill $w 2>/dev/null
  fi
  rm -f $PD/sysd.pid
  rm -rf "$R/run/phonedesktop/sysd"     # tells pd-dbus-helper that pd-sysd is gone
fi
if [ -n "$UM" ] && [ $PD_MODE = root ]; then sh $PD/bin/pd-mount.sh umount; echo "stopped and unmounted"; else echo "stopped"; fi

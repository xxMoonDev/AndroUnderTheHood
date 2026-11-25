#!/system/bin/sh
# Start Xvfb + KDE Plasma + RustDesk under proot (shell user, no root).
. /data/local/tmp/pdnr/bin/pd.conf
[ -x "$R/usr/local/sbin/pd-desktop" ] || { echo "not installed"; exit 1; }
for j in $PD/jobs/*.pid; do
  [ -f "$j" ] || continue
  n=${j%.pid}
  [ -f "$n.rc" ] || ! kill -0 "$(cat "$j")" 2>/dev/null || { echo "a '${n##*/}' job is running - wait for it first"; exit 1; }
done
LOG=$PD/desktop.log
: > $LOG
# proot lives as long as anything it started, so the desktop's proot runs detached
setsid sh $PD/bin/pdn-proot.sh /usr/bin/env PD_USER="$PD_USER" PD_UID="$PD_UID" PD_RES="$PD_RES" \
  PD_DISPLAY="$PD_DISPLAY" /usr/local/sbin/pd-desktop start </dev/null >>$LOG 2>&1 &
i=0
while [ $i -lt 240 ]; do
  grep -q '^started:' $LOG && { cat $LOG; exit 0; }
  i=$((i+1)); sleep 0.5
done
cat $LOG
echo "the desktop did not report 'started' within 2 minutes"
exit 1

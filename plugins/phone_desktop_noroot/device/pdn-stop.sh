#!/system/bin/sh
# Stop the Ubuntu desktop (shell user, no root).
#   pdn-stop.sh         shut down: Plasma logs out cleanly, then everything stops
#   pdn-stop.sh --now   power off: skip the logout, stop everything immediately
# SAFETY: as the shell user we share the process list with other shell-user processes
# (adb sessions, Shizuku's server). Never kill by name or by user. Only processes whose
# executable is our proot or proot's loader copy (both inside $PD) are ours.
. /data/local/tmp/pdnr/bin/pd.conf
NOW=
for a in "$@"; do case "$a" in --now) NOW=1 ;; esac; done
for j in $PD/jobs/*.pid; do
  [ -f "$j" ] || continue
  n=${j%.pid}
  [ -f "$n.rc" ] || ! kill -0 "$(cat "$j")" 2>/dev/null || { echo "a '${n##*/}' job is running - stop it first"; exit 1; }
done
# pids whose executable is inside $PD/tmp (proot's loader = every process in Ubuntu) or $PROOT
tracees() { ls -l /proc/[0-9]*/exe 2>/dev/null | grep " -> $PD/tmp/prooted-" | sed 's|.* /proc/\([0-9]*\)/exe .*|\1|'; }
proots()  { ls -l /proc/[0-9]*/exe 2>/dev/null | grep " -> $PROOT\$" | sed 's|.* /proc/\([0-9]*\)/exe .*|\1|'; }
if [ -z "$NOW" ] && [ -n "$(tracees)" ]; then
  sh $PD/bin/pdn-proot.sh /usr/bin/env PD_USER="$PD_USER" PD_UID="$PD_UID" PD_DISPLAY="$PD_DISPLAY" \
    /usr/local/sbin/pd-desktop logout
fi
p=$(tracees); [ -n "$p" ] && kill -TERM $p 2>/dev/null
for i in 1 2 3 4 5 6; do [ -z "$(tracees)" ] && break; sleep 1; done
p=$(tracees); [ -n "$p" ] && kill -KILL $p 2>/dev/null
sleep 1
p=$(proots); [ -n "$p" ] && kill -TERM $p 2>/dev/null
sleep 1
p=$(proots); [ -n "$p" ] && kill -KILL $p 2>/dev/null
echo stopped

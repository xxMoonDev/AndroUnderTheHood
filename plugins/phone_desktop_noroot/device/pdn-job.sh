#!/system/bin/sh
# Detached job runner (shell user):  pdn-job.sh NAME COMMAND [ARGS...]
# Starts COMMAND in its own session, so it survives the USB/SSH connection
# dropping. Output goes to $J/NAME.log, the exit code to $J/NAME.rc when it
# ends, the pid to $J/NAME.pid. Clients follow the log with
#   tail -c +OFFSET $J/NAME.log      and check   $J/NAME.rc
#   pdn-job.sh --status NAME   -> "running PID" | "done RC" | "none"
#   pdn-job.sh --kill NAME     -> stops the job and its children
J=/data/local/tmp/pdnr/jobs
mkdir -p $J
case "$1" in
  --status)
    n=$2
    if [ -f $J/$n.rc ]; then echo "done $(cat $J/$n.rc)"
    elif [ -f $J/$n.pid ] && kill -0 "$(cat $J/$n.pid)" 2>/dev/null; then echo "running $(cat $J/$n.pid)"
    elif [ -f $J/$n.log ]; then echo "done 255"     # vanished without an exit code (reboot, killed)
    else echo none; fi
    exit 0 ;;
  --kill)
    n=$2; p=$(cat $J/$n.pid 2>/dev/null)
    if [ -n "$p" ]; then
      # the job leads its own session: kill the whole session
      for q in $(ps -A -o PID,SID | awk -v s="$p" '$2==s{print $1}'); do kill -TERM $q 2>/dev/null; done
      sleep 1; kill -KILL -- -$p 2>/dev/null; kill -KILL $p 2>/dev/null
    fi
    [ -f $J/$n.rc ] || echo 143 > $J/$n.rc
    echo killed; exit 0 ;;
esac
n=$1; shift
if [ -f $J/$n.pid ] && [ ! -f $J/$n.rc ] && kill -0 "$(cat $J/$n.pid)" 2>/dev/null; then
  echo "already running $(cat $J/$n.pid)"; exit 3
fi
rm -f $J/$n.rc $J/$n.log $J/$n.pid
setsid sh -c '"$@" > "$0.log" 2>&1; echo $? > "$0.rc"' "$J/$n" "$@" </dev/null >/dev/null 2>&1 &
echo $! > $J/$n.pid
echo "started $!"

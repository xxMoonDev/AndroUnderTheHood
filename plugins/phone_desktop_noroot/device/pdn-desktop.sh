#!/bin/bash
# Runs INSIDE Ubuntu under proot:  pd-desktop start|logout|status
# Env (from pdn-start.sh): PD_USER PD_UID PD_RES PD_DISPLAY PD_RDPORT
# The no-root twin of PhoneDesktop's pd-desktop. It never kills anything: under proot the
# process list is Android's, shared with other shell-user processes (adb sessions,
# Shizuku). Stopping is done from the Android side by pdn-stop.sh, which only touches
# processes whose executable lives in the install folder.
PD_USER=${PD_USER:-kde}; PD_UID=${PD_UID:-1000}
PD_RES=${PD_RES:-1920x1080}; PD_DISPLAY=${PD_DISPLAY:-2}
DISP=":$PD_DISPLAY"
LOG=/var/log/phonedesktop; mkdir -p $LOG
RUNDIR=/run/user/$PD_UID
UHOME=$(getent passwd "$PD_USER" | cut -d: -f6)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=en_US.UTF-8
# Snapdragon 8-series: llvmpipe's JIT dies with SIGILL -> software Qt Quick + softpipe
export QT_QUICK_BACKEND=software GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1
# proot: Qt's QML JIT cannot get executable memory its way (mprotect EACCES) -> interpreter
export QV4_FORCE_INTERPRETER=1

# ours: a process that carries our environment marker (every process we start does)
ours() { tr '\0' '\n' < /proc/$1/environ 2>/dev/null | grep -qx 'PD_NOROOT=1'; }
running() {
  local p
  for p in $(pgrep -f -- "$1" 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    ours "$p" && return 0
  done
  return 1
}
bg() { # logname cmd...   -> fully detached daemon
  local l=$1; shift
  setsid "$@" </dev/null >>"$LOG/$l.log" 2>&1 &
}
user_env() {
  echo HOME="$UHOME" USER="$PD_USER" LOGNAME="$PD_USER" SHELL=/bin/bash PATH="$PATH" LANG="$LANG" \
    DISPLAY="$DISP" XDG_RUNTIME_DIR="$RUNDIR" XDG_SESSION_TYPE=x11 \
    XDG_CURRENT_DESKTOP=KDE XDG_SESSION_DESKTOP=KDE KDE_FULL_SESSION=true \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNDIR/bus" QT_QUICK_BACKEND="$QT_QUICK_BACKEND" \
    GALLIUM_DRIVER="$GALLIUM_DRIVER" LIBGL_ALWAYS_SOFTWARE=1 QV4_FORCE_INTERPRETER=1 PD_NOROOT=1
}
as_user() { # logname "shell command", run in the desktop session environment
  bg "$1" runuser -u "$PD_USER" -- env -i $(user_env) bash -c "$2"
}

start() {
  # leftovers of an earlier run that ended without a clean stop (phone reboot, kill):
  # nothing of ours runs now (pdn-start.sh checks), so the sockets and locks are stale
  if ! running "Xvfb $DISP"; then
    rm -rf /tmp/.X$PD_DISPLAY-lock /tmp/.X11-unix/X$PD_DISPLAY /run/dbus/pid /run/dbus/system_bus_socket "$RUNDIR"
  fi
  mkdir -p /run/dbus /tmp/.X11-unix; chmod 1777 /tmp /tmp/.X11-unix
  if ! running "dbus-daemon --system"; then
    # started directly as messagebus: as (proot's fake) root it would try to drop
    # capabilities, which fails with EPERM and ends the daemon
    rm -f /run/dbus/pid /run/dbus/system_bus_socket
    chown messagebus: /run/dbus
    runuser -u messagebus -- dbus-daemon --system --fork
  fi
  if ! running "Xvfb $DISP"; then
    bg xvfb Xvfb $DISP -screen 0 ${PD_RES}x24 -nolisten tcp -ac +extension GLX +extension RANDR
    for i in $(seq 100); do [ -S /tmp/.X11-unix/X$PD_DISPLAY ] && break; sleep 0.2; done
  fi
  mkdir -p $RUNDIR; chown $PD_USER: $RUNDIR; chmod 700 $RUNDIR
  mkdir -p /run/phonedesktop
  printf 'USER_=%s\nUID_=%s\nDISPLAY_=%s\n' "$PD_USER" "$PD_UID" "$DISP" > /run/phonedesktop/session
  if ! running "dbus-daemon --session --address=unix:path=$RUNDIR/bus"; then
    rm -f $RUNDIR/bus
    runuser -u $PD_USER -- env -i $(user_env) dbus-daemon --session --address=unix:path=$RUNDIR/bus --fork --nopidfile
  fi
  if ! running "pulseaudio --daemonize=no"; then
    as_user pulseaudio "exec pulseaudio --daemonize=no --exit-idle-time=-1 --log-target=stderr"
    for i in $(seq 60); do [ -S "$RUNDIR/pulse/native" ] && break; sleep 0.25; done
  fi
  running "startplasma-x11" || as_user plasma "exec startplasma-x11"
  running "rustdesk --server" || as_user rustdesk "LD_PRELOAD=/usr/local/lib/pd-shmshim.so exec rustdesk --server"
  echo "started: display $DISP ${PD_RES}"
}

logout() { # graceful: ask Plasma to log out (apps get to save), give it 20 s
  if running startplasma-x11; then
    echo "asking Plasma to log out..."
    runuser -u "$PD_USER" -- env -i $(user_env) timeout 10 qdbus org.kde.ksmserver /KSMServer logout 0 0 0 >/dev/null 2>&1
    for i in $(seq 20); do running startplasma-x11 || break; sleep 1; done
    running startplasma-x11 && echo "Plasma did not log out in time"
  fi
  return 0
}

status() {
  for p in "dbus-daemon --system" "Xvfb $DISP" "pulseaudio --daemonize=no" startplasma-x11 plasmashell kwin_x11 "rustdesk --server"; do
    if running "$p"; then echo "  [up]   $p"; else echo "  [down] $p"; fi
  done
}

case "$1" in
  start) start ;;
  logout) logout ;;
  status) status ;;
  *) echo "usage: pd-desktop start|logout|status"; exit 2 ;;
esac

#!/bin/bash
# Runs INSIDE Ubuntu (root mode: as root; proot mode: as the desktop user):
#   pd-desktop start|stop|logout|status
# Env (from pd-start.sh): PD_MODE PD_USER PD_UID PD_RES PD_DISPLAY [PD_SUPER]
PD_USER=${PD_USER:-kde}; PD_UID=${PD_UID:-5999}
PD_RES=${PD_RES:-1920x1080}; PD_DISPLAY=${PD_DISPLAY:-1}
DISP=":$PD_DISPLAY"
LOG=/var/log/phonedesktop; mkdir -p $LOG
RUNDIR=/run/user/$PD_UID
UHOME=$(getent passwd "$PD_USER" | cut -d: -f6)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=en_US.UTF-8
# Mesa's llvmpipe JIT dies with SIGILL on Snapdragon 8-series cores (LLVM
# assumes SVE from the core type, but the kernel has SVE disabled). Keep Qt
# Quick (Plasma) off OpenGL and give any other GL user the non-JIT softpipe.
export QT_QUICK_BACKEND=software GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1

# SAFETY: the chroot shares Android's process list. Never kill by name or by user
# alone - Android's own processes would match (uid 1000 is Android's "system":
# `pkill -u` on it takes down system_server and the whole phone). Only touch
# processes INSIDE this chroot. readlink /proc/PID/root says "/" for Android's
# processes too (tested), so compare the root DIRECTORIES by device:inode.
if [ "$PD_MODE" = proot ]; then
  # proot mode (no root): every desktop process is traced by one proot "supervisor". Inside it,
  # that is our own tracer; from another proot (status, logout) pd-stop/pd-status pass its pid.
  PD_SUPER=${PD_SUPER:-$(awk '/^TracerPid:/{print $2}' /proc/$$/status)}
  in_chroot() { grep -q "^TracerPid:[[:space:]]*$PD_SUPER\$" /proc/$1/status 2>/dev/null; }
  # everything really runs as the shell user, which is the desktop user here: no switching
  ASU=""
else
  CHROOT_ID=$(stat -L -c '%d:%i' /proc/$$/root/ 2>/dev/null)
  in_chroot() { [ -n "$CHROOT_ID" ] && [ "$(stat -L -c '%d:%i' /proc/$1/root/ 2>/dev/null)" = "$CHROOT_ID" ]; }
  ASU="runuser -u $PD_USER --"
fi
# run as the desktop user (a command prefix, so it also works after setsid)
asu() { $ASU "$@"; }
chroot_pgrep() { # pgrep args... -> matching pids inside this chroot
  for p in $(pgrep "$@" 2>/dev/null); do
    [ "$p" = "$$" ] && continue
    in_chroot "$p" && echo "$p"
  done
}
running() { [ -n "$(chroot_pgrep -f -- "$1")" ]; }
ckill() { # signal pgrep-args...
  local sig=$1; shift
  local p; p=$(chroot_pgrep "$@")
  [ -n "$p" ] && kill "-$sig" $p 2>/dev/null
  return 0
}
bg() { # logname cmd...   -> fully detached daemon
  local l=$1; shift
  setsid "$@" </dev/null >>"$LOG/$l.log" 2>&1 &
}
as_user() { # logname "shell command", run in the desktop session environment
  bg "$1" $ASU env -i HOME="$UHOME" \
    USER="$PD_USER" LOGNAME="$PD_USER" SHELL=/bin/bash PATH="$PATH" LANG="$LANG" \
    DISPLAY="$DISP" XDG_RUNTIME_DIR="$RUNDIR" XDG_SESSION_TYPE=x11 \
    XDG_CURRENT_DESKTOP=KDE XDG_SESSION_DESKTOP=KDE KDE_FULL_SESSION=true \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNDIR/bus"     QT_QUICK_BACKEND="$QT_QUICK_BACKEND" GALLIUM_DRIVER="$GALLIUM_DRIVER"     LIBGL_ALWAYS_SOFTWARE=1 bash -c "$2"
}

start() {
  # system D-Bus (polkit, udisks, ... clients expect it). Not in proot mode: it has to switch
  # to the messagebus user, which needs real root; the session bus below is what matters.
  mkdir -p /run/dbus
  if [ "$PD_MODE" != proot ] && ! running "dbus-daemon --system"; then
    rm -f /run/dbus/pid /run/dbus/system_bus_socket
    dbus-daemon --system --fork
  fi
  # proot mode: a system bus run as the desktop user (no messagebus switch, no setuid launch
  # helper). Root services (PackageKit + polkit, for Discover) are started by pd-sysd, which runs
  # under fake root: our helper hands it the request. See pd-sysd.sh for why the bus stays here.
  SYSCONF=/run/phonedesktop/system-bus.conf
  if [ "$PD_MODE" = proot ] && [ -x /usr/local/lib/phonedesktop/pd-dbus-helper ] &&
     ! running "dbus-daemon --config-file=$SYSCONF"; then
    mkdir -p /run/phonedesktop
    rm -f /run/dbus/pid /run/dbus/system_bus_socket
    sed -e '/<user>/d' -e '/<pidfile>/d' \
      -e 's#<includedir>system.d</includedir>#<includedir>/usr/share/dbus-1/system.d</includedir>#' \
      -e 's#</busconfig>#<servicehelper>/usr/local/lib/phonedesktop/pd-dbus-helper</servicehelper><policy context="mandatory"><allow own="org.freedesktop.PackageKit"/><allow own="org.freedesktop.PolicyKit1"/></policy></busconfig>#' \
      /usr/share/dbus-1/system.conf > $SYSCONF
    bg sysbus dbus-daemon --config-file=$SYSCONF --nofork --nopidfile
    for i in $(seq 40); do [ -e /run/dbus/system_bus_socket ] && break; sleep 0.25; done
  fi
  # Discover / PackageKit: let the desktop user install and remove software without a password
  # prompt. The prompt can't work here: KDE's polkit agent needs a login session from
  # systemd-logind, which a chroot/proot doesn't have, so Discover would only say "Failed to
  # obtain authentication". (Same trust as the desktop login itself: whoever has the desktop
  # already has a sudo-capable user.)
  if [ -d /usr/share/polkit-1 ]; then
    mkdir -p /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/49-phonedesktop.rules <<EOF
// PhoneDesktop: software installs from Discover without a password prompt
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.freedesktop.packagekit.") == 0 && subject.user == "$PD_USER")
    return polkit.Result.YES;
});
EOF
  fi
  # virtual X display
  if ! running "Xvfb $DISP"; then
    rm -f /tmp/.X$PD_DISPLAY-lock /tmp/.X11-unix/X$PD_DISPLAY
    bg xvfb Xvfb $DISP -screen 0 ${PD_RES}x24 -nolisten tcp -ac +extension GLX +extension RANDR
    for i in $(seq 50); do [ -e /tmp/.X11-unix/X$PD_DISPLAY ] && break; sleep 0.2; done
  fi
  mkdir -p $RUNDIR; chown $PD_USER: $RUNDIR 2>/dev/null; chmod 700 $RUNDIR
  # session description for the loginctl stand-in (RustDesk reads seat0)
  mkdir -p /run/phonedesktop
  printf 'USER_=%s\nUID_=%s\nDISPLAY_=%s\n' "$PD_USER" "$PD_UID" "$DISP" > /run/phonedesktop/session
  # session D-Bus shared by Plasma and RustDesk
  if ! running "dbus-daemon --session --address=unix:path=$RUNDIR/bus"; then
    rm -f $RUNDIR/bus
    asu dbus-daemon --session --address=unix:path=$RUNDIR/bus --fork --nopidfile
  fi
  # Sound: PulseAudio as the desktop user. The chroot has no sound card (Android owns it), so
  # PulseAudio creates a virtual output "auto_null"; RustDesk records its monitor and plays it
  # on the connected PC. Started before Plasma so the volume applet and apps find it.
  if ! running "pulseaudio --daemonize=no"; then
    as_user pulseaudio "exec pulseaudio --daemonize=no --exit-idle-time=-1 --log-target=stderr"
    for i in $(seq 40); do [ -e "$RUNDIR/pulse/native" ] && break; sleep 0.25; done
  fi
  # KDE Plasma (X11 session on the virtual display)
  running "startplasma-x11" || as_user plasma "exec startplasma-x11"
  # RustDesk server process (screen capture + input + direct IP listener)
  running "rustdesk --server" || as_user rustdesk "LD_PRELOAD=/usr/local/lib/pd-shmshim.so exec rustdesk --server"
  # Android apps in the start menu (set up from the plugin): keep the menu current
  if [ -x /usr/local/bin/android-apps-watch ] && [ -f "$UHOME/.config/android-apps/config" ]; then
    running "android-apps-watch" || as_user androidapps "exec /usr/local/bin/android-apps-watch"
  fi
  echo "started: display $DISP ${PD_RES}"
}

logout() {
  # graceful: ask Plasma to log out (apps get to save, session is stored), give it 20 s
  if running startplasma-x11; then
    echo "asking Plasma to log out..."
    asu env -i HOME="$UHOME" PATH="$PATH" DISPLAY="$DISP" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNDIR/bus" \
      timeout 10 qdbus org.kde.ksmserver /KSMServer logout 0 0 0 >/dev/null 2>&1
    for i in $(seq 20); do running startplasma-x11 || break; sleep 1; done
    running startplasma-x11 && echo "Plasma did not log out in time - stopping it"
  fi
}

stop() {
  logout
  ckill TERM -f -- "rustdesk --"; ckill TERM -f -- startplasma-x11; ckill TERM -U "$PD_UID"
  sleep 2
  ckill KILL -U "$PD_UID"; ckill TERM -f -- "Xvfb $DISP"; ckill TERM -f -- "dbus-daemon --system"
}

status() {
  local sysbus="dbus-daemon --system"
  for p in "$sysbus" "Xvfb $DISP" "pulseaudio --daemonize=no" startplasma-x11 plasmashell kwin_x11 "rustdesk --server"; do
    [ "$PD_MODE" = proot ] && [ "$p" = "$sysbus" ] && p="dbus-daemon --config-file=/run/phonedesktop/system-bus.conf"
    if running "$p"; then echo "  [up]   $p"; else echo "  [down] $p"; fi
  done
}

case "$1" in
  start) start ;;
  stop) stop ;;
  logout) logout ;;
  status) status ;;
  *) echo "usage: pd-desktop start|stop|logout|status"; exit 2 ;;
esac

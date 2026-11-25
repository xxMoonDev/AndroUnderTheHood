#!/system/bin/sh
# Phone <-> Ubuntu bridge (root, su -mm):  pd-bridge.sh start|stop|status|config KEY=VALUE...
#   clipboard      Android clipboard <-> Plasma clipboard
#   notifications  Android notifications -> Plasma popups
#   audio          Ubuntu's sound -> the phone's speaker (PulseAudio stream on 127.0.0.1:4713)
#   autostart      start the bridge together with the desktop (pd-start.sh)
# Settings live in $PD/bridge.conf (BRIDGE_CLIPBOARD, BRIDGE_NOTIFICATIONS, BRIDGE_AUDIO, BRIDGE_AUTOSTART = 0|1).
# The helper is a class inside the widget app, run with app_process as root.
. "${0%/*}/pd.conf"
CONF=$PD/bridge.conf
LOG=$PD/bridge.log
PORT=4713
CLASS=dev.phonedesktop.widget.Bridge
NAME=pd-bridge                 # the helper's process name (app_process --nice-name)
PIDF=$PD/bridge.pid
BRIDGE_CLIPBOARD=1; BRIDGE_NOTIFICATIONS=1; BRIDGE_AUDIO=0; BRIDGE_AUTOSTART=0
[ -f $CONF ] && . $CONF

as_user() {   # run a command in the desktop session
  chroot "$R" /usr/sbin/runuser -u "$PD_USER" -- /usr/bin/env -i HOME=/home/$PD_USER PATH=/usr/bin:/bin \
    XDG_RUNTIME_DIR=/run/user/$PD_UID DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$PD_UID/bus "$@"
}

pulse_stream() {   # on|off: PulseAudio's raw-PCM stream of the desktop output, localhost only
  if [ "$1" = on ]; then
    as_user pactl list short modules 2>/dev/null | grep -q module-simple-protocol-tcp ||
      as_user pactl load-module module-simple-protocol-tcp rate=48000 format=s16le channels=2 \
        source=auto_null.monitor record=true playback=false port=$PORT listen=127.0.0.1 >/dev/null
  else
    as_user pactl unload-module module-simple-protocol-tcp >/dev/null 2>&1
  fi
}

# only ever the pid we started, and only while it still is our helper
bridge_pid() {
  p=$(cat $PIDF 2>/dev/null); [ -n "$p" ] || return 1
  tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "^$NAME" && echo $p
}
running() { bridge_pid >/dev/null; }

case "$1" in
  start)
    running && { echo "bridge already running"; exit 0; }
    apk=$(pm path dev.phonedesktop.widget 2>/dev/null | head -n1 | cut -d: -f2)
    [ -n "$apk" ] || { echo "bridge: the widget app is not installed"; exit 1; }
    ls -l /proc/[0-9]*/root 2>/dev/null | grep -q " -> $R\$" || { echo "bridge: the desktop is not running"; exit 1; }
    args="--rootfs $R --user $PD_USER --uid $PD_UID --display :$PD_DISPLAY"
    [ "$BRIDGE_CLIPBOARD" = 1 ] && args="$args --clipboard"
    [ "$BRIDGE_NOTIFICATIONS" = 1 ] && args="$args --notifications"
    if [ "$BRIDGE_AUDIO" = 1 ]; then pulse_stream on; args="$args --audio $PORT"; else pulse_stream off; fi
    CLASSPATH=$apk setsid app_process /system/bin --nice-name=$NAME $CLASS $args </dev/null >$LOG 2>&1 &
    echo $! > $PIDF
    sleep 2
    running && echo "bridge started (clipboard=$BRIDGE_CLIPBOARD notifications=$BRIDGE_NOTIFICATIONS audio=$BRIDGE_AUDIO)" ||
      { echo "bridge failed to start:"; tail -n 5 $LOG; exit 1; }
    ;;
  stop)
    p=$(bridge_pid) && kill $p
    rm -f $PIDF
    pulse_stream off
    echo "bridge stopped"
    ;;
  status)
    if running; then echo "bridge running (clipboard=$BRIDGE_CLIPBOARD notifications=$BRIDGE_NOTIFICATIONS audio=$BRIDGE_AUDIO)"
    else echo "bridge stopped"; fi
    tail -n 5 $LOG 2>/dev/null
    ;;
  config)
    shift
    for kv in "$@"; do
      case "$kv" in
        clipboard=[01]) BRIDGE_CLIPBOARD=${kv#*=} ;;
        notifications=[01]) BRIDGE_NOTIFICATIONS=${kv#*=} ;;
        audio=[01]) BRIDGE_AUDIO=${kv#*=} ;;
        autostart=[01]) BRIDGE_AUTOSTART=${kv#*=} ;;
        *) echo "unknown setting $kv"; exit 2 ;;
      esac
    done
    printf 'BRIDGE_CLIPBOARD=%s\nBRIDGE_NOTIFICATIONS=%s\nBRIDGE_AUDIO=%s\nBRIDGE_AUTOSTART=%s\n' \
      "$BRIDGE_CLIPBOARD" "$BRIDGE_NOTIFICATIONS" "$BRIDGE_AUDIO" "$BRIDGE_AUTOSTART" > $CONF
    echo "saved: clipboard=$BRIDGE_CLIPBOARD notifications=$BRIDGE_NOTIFICATIONS audio=$BRIDGE_AUDIO autostart=$BRIDGE_AUTOSTART"
    ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac

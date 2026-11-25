#!/bin/bash
# Installed in Ubuntu as /usr/local/bin/android-apps-watch (PhoneDesktop "Android apps").
# Keeps the Android start menu current: every WATCH_INTERVAL seconds (default 60) it asks the phone for
# its package list over SSH and runs android-apps-sync when the list changed (app installed / removed /
# updated). Started by pd-desktop with the desktop; one instance per user.
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/android-apps/config
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/android-apps
mkdir -p "$CACHE"
PIDF=$CACHE/watch.pid
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && \
   grep -q android-apps-watch "/proc/$(cat "$PIDF")/cmdline" 2>/dev/null; then
  echo "already running"; exit 0
fi
echo $$ > "$PIDF"
last=""
while true; do
  if [ -f "$CONF" ]; then
    . "$CONF"
    user=${SSH_TARGET%%@*}; hp=${SSH_TARGET#*@}; host=${hp%%:*}; port=${hp##*:}
    [ "$port" = "$hp" ] && port=22
    # package names + versions: changes on install, removal and update
    root=; [ "${SSH_ROOT:-0}" = 1 ] && root="-o SetEnv=ROOTSSH_ROOT=1"
    h=$(ssh $root -T -i "$SSH_KEY" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" -o StrictHostKeyChecking=yes \
          -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR -p "$port" "$user@$host" \
          "cmd package list packages --show-versioncode 2>/dev/null" 2>/dev/null | sort | md5sum | cut -c1-32)
    # an empty answer (phone's SSH server not running) hashes to d41d8cd9...: skip it
    if [ -n "$h" ] && [ "$h" != d41d8cd98f00b204e9800998ecf8427e ] && [ "$h" != "$last" ]; then
      if /usr/local/bin/android-apps-sync >> "$CACHE/sync.log" 2>&1; then last=$h; fi
    fi
  fi
  sleep "${WATCH_INTERVAL:-60}"
done

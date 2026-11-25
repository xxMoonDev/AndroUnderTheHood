#!/system/bin/sh
# PhoneDesktop "Android apps": open this phone's apps inside Ubuntu (scrcpy over SSH) and list them in
# the Plasma start menu.  Android side; root mode (su -mm) or no-root mode (adb shell user).
#   pd-android-setup.sh setup DIR   install scrcpy-ssh + helper from DIR, make the desktop user's SSH key,
#                                   authorize it in AndroOpenHeart, write the config, fill the menu
#   pd-android-setup.sh sync        refresh the start menu now
#   pd-android-setup.sh status      show what is set up
#   pd-android-setup.sh remove      take the menu entries, config and watcher away (scrcpy-ssh stays)
# The SSH key is authorized through AndroOpenHeart's AdbSetupReceiver (1.4+), which only the shell user
# or root can call. AndroOpenHeart loads keys when its server starts, so it restarts once when the key
# is new (open SSH sessions to the phone drop then; ADB is not affected).
. "${0%/*}/pd.conf"
UHOME=/home/$PD_USER
HKEY=$UHOME/.ssh/android_apps
CONFD=$UHOME/.config/android-apps
AOH=dev.rootssh/.AdbSetupReceiver

as_user() { # run a shell command in Ubuntu as the desktop user
  if [ $PD_MODE = root ]; then
    chroot "$R" /usr/sbin/runuser -u "$PD_USER" -- /usr/bin/env -i HOME=$UHOME USER=$PD_USER \
      PATH=/usr/local/bin:/usr/bin:/bin DISPLAY=:$PD_DISPLAY /bin/bash -c "$1"
  else
    pd_in_user /usr/bin/env -i HOME=$UHOME USER=$PD_USER PATH=/usr/local/bin:/usr/bin:/bin \
      DISPLAY=:$PD_DISPLAY /bin/bash -c "$1"
  fi
}
aoh() { # AndroOpenHeart setup broadcast; prints its key=value result lines
  am broadcast -a dev.rootssh.action.SETUP -n $AOH "$@" 2>&1 | sed -n 's/.*data="//; /=/p' | tr -d '"'
}
listening() { # is anything listening on TCP port $1 (localhost or all)?
  hex=$(printf '%04X' "$1")
  grep -qi ":$hex [0-9A-F]*:0000 0A" /proc/net/tcp /proc/net/tcp6 2>/dev/null
}
watcher_running() {
  as_user 'p=$(cat ~/.cache/android-apps/watch.pid 2>/dev/null); [ -n "$p" ] && grep -q android-apps-watch /proc/$p/cmdline 2>/dev/null'
}

case "$1" in
setup)
  D=${2:-/data/local/tmp/phonedesktop}
  [ -x "$R/usr/local/sbin/pd-desktop" ] || { echo "Install the desktop first."; exit 1; }
  pm path dev.rootssh >/dev/null 2>&1 || { echo "AndroOpenHeart (dev.rootssh) is not installed on this phone."; exit 1; }
  echo "== scrcpy-ssh and the app helper"
  [ -f "$D/scrcpy-ssh-linux-arm64.tar.gz" ] && { $TAR -xzf "$D/scrcpy-ssh-linux-arm64.tar.gz" -C "$R" || exit 1; }
  [ -x "$R/opt/scrcpy-ssh/bin/scrcpy" ] || { echo "scrcpy-ssh is missing in $D"; exit 1; }
  mkdir -p "$R/usr/local/share/phonedesktop"
  cp "$D/pd-android-apps.jar" "$R/usr/local/share/phonedesktop/pd-android-apps.jar" || exit 1
  cp "$D/pd-android-app.sh"   "$R/usr/local/bin/android-app"
  cp "$D/pd-android-sync.sh"  "$R/usr/local/bin/android-apps-sync"
  cp "$D/pd-android-watch.sh" "$R/usr/local/bin/android-apps-watch"
  chmod 755 "$R/usr/local/bin/android-app" "$R/usr/local/bin/android-apps-sync" "$R/usr/local/bin/android-apps-watch"
  # proot mode: the newest socket shim (PulseAudio needs it for sound, also in the app windows)
  if [ $PD_MODE = proot ] && [ -f "$D/pd-unixshim.c" ] && ! cmp -s "$D/pd-unixshim.c" "$R/usr/local/src/pd-unixshim.c"; then
    echo "   updating the socket shim (sound in no-root mode; restart the desktop to use it)"
    cp "$D/pd-unixshim.c" "$R/usr/local/src/pd-unixshim.c"
    sh $PD/bin/pd-shell.sh /bin/sh -c 'S=/usr/local/lib/pd-unixshim.so; gcc -O2 -Wall -shared -fPIC -o $S.new /usr/local/src/pd-unixshim.c -ldl && LD_PRELOAD=$S.new /bin/true && mv $S.new $S' \
      || echo "   (could not rebuild the shim - the old one stays)"
  fi
  # scrcpy's runtime libraries (FFmpeg, libusb) and the ssh client, when not there yet
  need=
  [ -x "$R/usr/bin/ssh" ] || need="openssh-client"
  if as_user 'LD_LIBRARY_PATH=/opt/scrcpy-ssh/lib ldd /opt/scrcpy-ssh/bin/scrcpy' 2>/dev/null | grep -q 'not found'; then
    need="$need libavcodec60 libavformat60 libavutil58 libswresample4 libusb-1.0-0"
  fi
  if [ -n "$need" ]; then
    echo "   installing in Ubuntu:$need"
    sh $PD/bin/pd-shell.sh /usr/bin/env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $need \
      >/dev/null 2>&1 || { echo "could not install$need (no network?)"; exit 1; }
  fi
  echo "   $(as_user '/opt/scrcpy-ssh/bin/scrcpy --version 2>/dev/null | head -n1')"

  echo "== SSH key of the desktop user"
  as_user 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && [ -f ~/.ssh/android_apps ] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/android_apps -C phonedesktop-android-apps'
  PUB=$(cat "$R$HKEY.pub" 2>/dev/null)
  case "$PUB" in ssh-ed25519\ *) echo "   ${PUB%% *} ...${PUB##* }" ;; *) echo "no key was made"; exit 1 ;; esac

  echo "== AndroOpenHeart"
  res=$(aoh --es add_key "$PUB")
  echo "$res" | grep -q '^ok=true' || { echo "AndroOpenHeart did not answer the setup request - it needs version 1.4 or newer:"; echo "$res"; exit 1; }
  port=$(echo "$res" | sed -n 's/^port=//p'); user=$(echo "$res" | sed -n 's/^username=//p')
  hostkey=$(echo "$res" | sed -n 's/^hostkey=//p')
  [ -n "$port" ] && [ -n "$user" ] && [ -n "$hostkey" ] || { echo "incomplete answer: $res"; exit 1; }
  if echo "$res" | grep -q '^key=added' || ! listening "$port"; then
    echo "   key authorized - restarting AndroOpenHeart's SSH server so it loads the key"
    r2=$(aoh --ez start true); echo "   $(echo "$r2" | grep '^start=')"
    for i in 1 2 3 4 5 6 7 8 9 10; do listening "$port" && break; sleep 1; done
  else
    echo "   key was already authorized"
  fi
  listening "$port" || echo "   WARNING: nothing listens on port $port yet - start AndroOpenHeart on the phone"
  echo "   $user@127.0.0.1:$port"

  mkdir -p "$R$CONFD"
  echo "[127.0.0.1]:$port $hostkey" > "$R$CONFD/known_hosts"
  if [ ! -f "$R$CONFD/config" ]; then
    printf '%s\n' "# PhoneDesktop Android apps (written by pd-android-setup.sh; your changes below the first 3 lines stay)" \
      "SSH_TARGET=$user@127.0.0.1:$port" "SSH_KEY=$HKEY" "KNOWN_HOSTS=$CONFD/known_hosts" \
      "DISPLAY_SIZE=1080x1920/320   # size/dpi of each app's virtual display" \
      "AUDIO=1                      # 0 = no sound from the apps" \
      "MAX_FPS=30" "BITRATE=6M" "WATCH_INTERVAL=60           # seconds between checks for installed/removed apps" > "$R$CONFD/config"
  else
    sed -i "s|^SSH_TARGET=.*|SSH_TARGET=$user@127.0.0.1:$port|" "$R$CONFD/config"
  fi
  # root mode: AndroOpenHeart runs plain sessions as its own app user, which can't start scrcpy's
  # server; with ROOTSSH_ROOT=1 (AndroOpenHeart 1.3+) it runs the session through its su.
  # No-root mode: sessions already run as the shell user (Shizuku).
  if [ $PD_MODE = root ]; then sshroot=1; else sshroot=0; fi
  if grep -q '^SSH_ROOT=' "$R$CONFD/config"; then sed -i "s|^SSH_ROOT=.*|SSH_ROOT=$sshroot|" "$R$CONFD/config"
  else echo "SSH_ROOT=$sshroot" >> "$R$CONFD/config"; fi
  [ $PD_MODE = root ] && chroot "$R" /bin/chown -R "$PD_USER:$PD_USER" "$CONFD" "$UHOME/.ssh"

  echo "== start menu"
  as_user '/usr/local/bin/android-apps-sync' || { echo "the first sync failed - see above"; exit 1; }
  if pd_running && ! watcher_running; then
    echo "   starting the watcher (refreshes the menu when apps are installed or removed)"
    if [ $PD_MODE = root ]; then
      chroot "$R" /usr/sbin/runuser -u "$PD_USER" -- /usr/bin/env -i HOME=$UHOME PATH=/usr/local/bin:/usr/bin:/bin \
        setsid /usr/local/bin/android-apps-watch </dev/null >/dev/null 2>&1 &
    else
      # its own small proot until the next desktop start (pd-stop.sh stops it too)
      setsid sh -c 'echo $$ > '"$PD"'/android-watch.pid; . '"$PD"'/bin/pd.conf; pd_proot_env;
        exec $PD/bin/proot $PROOT_ARGS /usr/bin/env -i HOME='"$UHOME"' PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/android-apps-watch' \
        </dev/null >/dev/null 2>&1 &
    fi
  fi
  echo "== Android apps ready: start menu > Android"
  ;;
sync)
  as_user '/usr/local/bin/android-apps-sync'
  ;;
status)
  if [ -f "$R$CONFD/config" ]; then
    echo "set up: $(sed -n 's/^SSH_TARGET=//p' "$R$CONFD/config")"
    echo "apps in the menu: $(ls "$R$UHOME"/.local/share/applications/android-app-*.desktop 2>/dev/null | grep -vc -- '--screen')"
    if watcher_running; then echo "watcher: running"; else echo "watcher: stopped"; fi
  else
    echo "not set up"
  fi
  [ -x "$R/opt/scrcpy-ssh/bin/scrcpy" ] && echo "scrcpy-ssh: installed" || echo "scrcpy-ssh: not installed"
  ;;
remove)
  as_user 'p=$(cat ~/.cache/android-apps/watch.pid 2>/dev/null); [ -n "$p" ] && grep -q android-apps-watch /proc/$p/cmdline 2>/dev/null && kill $p
    rm -f ~/.local/share/applications/android-app-*.desktop ~/.config/menus/applications-merged/phonedesktop-android.menu \
      ~/.local/share/desktop-directories/phonedesktop-android.directory
    rm -rf ~/.local/share/phonedesktop/android-icons ~/.config/android-apps ~/.cache/android-apps
    for k in kbuildsycoca6 kbuildsycoca5; do command -v $k >/dev/null && { $k >/dev/null 2>&1; break; }; done'
  if [ $PD_MODE = proot ]; then
    # the watcher's own proot (if setup started one): its traced processes first, then itself
    p=$(cat $PD/android-watch.pid 2>/dev/null)
    if [ -n "$p" ] && tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "$PD/bin/proot"; then
      t=$(pd_tracees $p); [ -n "$t" ] && kill $t 2>/dev/null; sleep 1; kill $p 2>/dev/null
    fi
    rm -f $PD/android-watch.pid
  fi
  echo "Android apps removed from the menu. The SSH key stays authorized in AndroOpenHeart (remove it there if you like)."
  ;;
*) sed -n '2,12p' "$0"; exit 2 ;;
esac

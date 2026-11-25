#!/bin/bash
# Installed in Ubuntu as /usr/local/bin/android-app (PhoneDesktop "Android apps").
#   android-app PACKAGE [LABEL]   open one Android app of this phone in its own window
#   android-app --screen          mirror the whole phone screen
# scrcpy-ssh reaches the phone over SSH (AndroOpenHeart on 127.0.0.1): every app gets its own
# virtual display, so several apps can be open side by side without touching the phone's screen.
# Settings: ~/.config/android-apps/config (written by the Phone desktop plugin).
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/android-apps/config
LOGDIR=${XDG_CACHE_HOME:-$HOME/.cache}/android-apps
mkdir -p "$LOGDIR"
fail() {
  if command -v kdialog >/dev/null && [ -n "$DISPLAY" ]; then kdialog --title "Android apps" --error "$1"
  else echo "android-app: $1" >&2; fi
  exit 1
}
[ -f "$CONF" ] || fail "Android apps aren't set up yet. In AndroUnderTheHood: Phone desktop > Android apps... > Set up."
. "$CONF"     # SSH_TARGET SSH_KEY KNOWN_HOSTS DISPLAY_SIZE AUDIO MAX_FPS BITRATE
S=/opt/scrcpy-ssh/bin/scrcpy
[ -x $S ] || fail "scrcpy-ssh is missing (/opt/scrcpy-ssh). Run Set up again in the Phone desktop plugin."

# ssh options for scrcpy-ssh, one per line: our key, the phone's host key, nothing interactive
SCRCPY_SSH_ARGS=$(printf '%s\n' -i "$SSH_KEY" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" \
  -o StrictHostKeyChecking=yes -o LogLevel=ERROR $([ "${SSH_ROOT:-0}" = 1 ] && echo -o SetEnv=ROOTSSH_ROOT=1))
export SCRCPY_SSH_ARGS
# software rendering (no GPU here), modest frame rate: the picture is encoded again for RustDesk
args=(--ssh "$SSH_TARGET" --render-driver=software --max-fps="${MAX_FPS:-30}" --video-bit-rate="${BITRATE:-6M}")
[ "${AUDIO:-1}" = 1 ] || args+=(--no-audio)

case "$1" in
  ""|-h|--help) sed -n '2,8p' "$0"; exit 0 ;;
  --screen)
    title="Phone screen"
    args+=(--window-title="$title" --stay-awake)
    ;;
  *)
    pkg=$1
    [[ $pkg =~ ^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$ ]] || fail "Not an Android package name: $pkg"
    title=${2:-$pkg}
    # its own virtual display at a phone-like size; the window can be resized
    args+=(--new-display="${DISPLAY_SIZE:-1080x1920/320}" --start-app="$pkg" --window-title="$title")
    # window class = package: the taskbar matches the menu entry (StartupWMClass) and its icon
    export SDL_APP_ID="$pkg"
    ;;
esac

log="$LOGDIR/${pkg:-screen}.log"
start=$(date +%s)
$S "${args[@]}" >"$log" 2>&1
rc=$?
# no sound server reachable: open the app without sound rather than not at all
if [ $rc -ne 0 ] && [ "${AUDIO:-1}" = 1 ] && grep -q 'Could not initialize SDL audio' "$log"; then
  mv "$log" "$log.audio-failed"
  start=$(date +%s)
  $S "${args[@]}" --no-audio >"$log" 2>&1
  rc=$?
fi
# a quick failure (no SSH, server not allowed to start...) gets a message; closing the window is rc 0
if [ $rc -ne 0 ] && [ $(( $(date +%s) - start )) -lt 20 ]; then
  fail "Could not open $title over SSH ($SSH_TARGET):
$(grep -E 'ERROR|error|denied|refused' "$log" | tail -n 4)

Is AndroOpenHeart running on the phone? Log: $log"
fi
exit $rc

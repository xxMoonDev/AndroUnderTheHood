#!/bin/bash
# Stand-in for systemd's loginctl inside the chroot (there is no logind).
# RustDesk asks loginctl who is logged in on seat0; without an answer it
# decides "No active console user logged on" and refuses file transfer.
# The session is described by /run/phonedesktop/session, written by
# `pd-desktop start` (USER= UID= DISPLAY=). Installed over /usr/bin/loginctl
# with dpkg-divert (the real one is kept as /usr/bin/loginctl.real).
S=/run/phonedesktop/session
[ -r "$S" ] && . "$S"
SID=c1

prop() { # name -> value
  case "$1" in
    Id) echo "$SID" ;;
    Name) echo "$USER" ;;
    User) echo "$UID_" ;;
    Seat) echo seat0 ;;
    Display) echo "$DISPLAY_" ;;
    Type) echo x11 ;;
    Class) echo user ;;
    State) echo active ;;
    Active) echo yes ;;
    Remote) echo no ;;
    TTY) echo tty7 ;;
    VTNr) echo 7 ;;
    Service) echo phonedesktop ;;
    Desktop) echo KDE ;;
    Leader) echo 1 ;;
    LockedHint|IdleHint) echo no ;;
    Sessions) echo "$SID" ;;
    *) echo "" ;;
  esac
}
UID_=${UID_:-}; USER=${USER_:-}; DISPLAY_=${DISPLAY_:-}

# no session file -> behave like "nobody logged in"
if [ -z "$USER" ]; then
  case "$1" in show-session|show-user|session-status|user-status) exit 1 ;; esac
  exit 0
fi

args=(); props=(); value=0
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--property) props+=("$2"); shift ;;
    --property=*) props+=("${1#*=}") ;;
    -P) props+=("$2"); value=1; shift ;;
    --value) value=1 ;;
    --no-legend|--no-pager|--no-ask-password|-l|--full|-a|--all) ;;
    -*) ;;
    *) args+=("$1") ;;
  esac
  shift
done
cmd=${args[0]:-list-sessions}

case "$cmd" in
  list-sessions)
    echo "$SID $UID_ $USER seat0 1 user tty7 no -" ;;
  list-users)
    echo "$UID_ $USER no active" ;;
  list-seats)
    echo "seat0" ;;
  show-session|show-user|show-seat|session-status|user-status)
    [ ${#props[@]} -eq 0 ] && props=(Id Name User Seat Display Type Class State Active Remote TTY)
    for p in "${props[@]}"; do
      IFS=, read -ra list <<< "$p"
      for q in "${list[@]}"; do
        if [ $value = 1 ]; then prop "$q"; else echo "$q=$(prop "$q")"; fi
      done
    done ;;
  *) exit 0 ;;
esac

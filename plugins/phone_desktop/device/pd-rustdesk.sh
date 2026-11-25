#!/system/bin/sh
# RustDesk settings for the chroot desktop (root, su -mm).
#   pd-rustdesk.sh config <mode> [server] [key]
#       mode: direct  - direct IP access on TCP 21118, no ID server (LAN/VPN only)
#             public  - RustDesk's public ID/relay servers (connect by ID)
#             both    - public ID servers + direct IP access
#             custom  - own hbbs at <server> with public <key>, + direct IP
#   pd-rustdesk.sh password <file>   set the permanent password from <file>
#                                    (file is deleted afterwards)
#   pd-rustdesk.sh id                print the RustDesk ID
. "${0%/*}/pd.conf"
sh $PD/bin/pd-mount.sh || exit 1
UHOME=$(pd_in /usr/bin/getent passwd "$PD_USER" | cut -d: -f6)
[ -n "$UHOME" ] || { echo "pd-rustdesk: user $PD_USER not found in Ubuntu" >&2; exit 1; }

rd() { # run rustdesk inside Ubuntu (talks to the server over IPC)
  if [ $PD_MODE = root ]; then     # as root, like the official service
    chroot "$R" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
      DISPLAY=":$PD_DISPLAY" /usr/bin/rustdesk "$@"
  else                             # proot: as the desktop user, the server's own user
    pd_in_user /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME="$UHOME" \
      DISPLAY=":$PD_DISPLAY" /usr/bin/rustdesk "$@"
  fi
}
server_up() {
  if [ $PD_MODE = proot ]; then
    for p in $(pd_tracees "$(pd_super)"); do
      tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q 'rustdesk --server' && return 0
    done
    return 1
  fi
  for p in $(pidof rustdesk 2>/dev/null); do
    [ "$(readlink /proc/$p/root 2>/dev/null)" = "$R" ] || continue
    tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q 'rustdesk --server' && return 0
  done
  return 1
}
# Server running: --option goes over IPC into the live config. Not running:
# write [options] in both config files directly, so the very first start
# already uses them (no contact with the public ID server in direct mode).
setopt() {
  if server_up; then rd --option "$1" "$2" >/dev/null 2>&1; return; fi
  for h in "$UHOME" /root; do
    f="$R$h/.config/rustdesk/RustDesk2.toml"
    mkdir -p "${f%/*}"
    [ -f "$f" ] || printf "rendezvous_server = ''\nnat_type = 0\nserial = 0\nunlock_pin = ''\n\n[options]\n" > "$f"
    grep -q '^\[options\]' "$f" || printf '\n[options]\n' >> "$f"
    sed -i "/^$1 = /d" "$f"
    sed -i "/^\[options\]/a $1 = '$2'" "$f"
  done
  [ $PD_MODE = root ] && chroot "$R" /bin/chown -R "$PD_USER:$PD_USER" "$UHOME/.config/rustdesk"
  return 0
}

case "$1" in
  config)
    mode=${2:-direct}
    case "$mode" in
      direct) setopt direct-server Y; setopt custom-rendezvous-server 127.0.0.1; setopt key "" ;;
      public) setopt direct-server N; setopt custom-rendezvous-server ""; setopt key "" ;;
      both)   setopt direct-server Y; setopt custom-rendezvous-server ""; setopt key "" ;;
      custom) setopt direct-server Y; setopt custom-rendezvous-server "$3"; setopt key "$4" ;;
      *) echo "unknown mode $mode" >&2; exit 2 ;;
    esac
    # unattended access: accept password logins, no click-to-accept dialog
    setopt verification-method use-permanent-password
    setopt approve-mode password
    echo "rustdesk mode: $mode"
    ;;
  password)
    f=$2
    if [ "$f" = - ]; then pw=$(cat)                 # from stdin (AndroUnderTheHood plugin)
    else
      [ -s "$f" ] || { echo "password file missing" >&2; exit 1; }
      pw=$(cat "$f"); rm -f "$f"
    fi
    [ -n "$pw" ] || { echo "empty password" >&2; exit 1; }
    out=$(rd --password "$pw" 2>&1); pw=
    echo "$out" | grep -qi done && echo "password set" || { echo "rustdesk: $out" >&2; exit 1; }
    ;;
  id)
    rd --get-id 2>/dev/null | tail -n1
    ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac

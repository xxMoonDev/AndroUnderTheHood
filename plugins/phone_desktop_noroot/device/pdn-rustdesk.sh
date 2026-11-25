#!/system/bin/sh
# RustDesk settings for the no-root desktop (shell user).
#   pdn-rustdesk.sh config <mode> [server] [key]
#       mode: direct  - direct IP access on TCP $PD_RDPORT, no ID server (LAN/VPN only)
#             public  - RustDesk's public ID/relay servers (connect by ID)
#             both    - public ID servers + direct IP access
#             custom  - own hbbs at <server> with public <key>, + direct IP
#   pdn-rustdesk.sh password -       set the permanent password read from stdin
#   pdn-rustdesk.sh id               print the RustDesk ID
. /data/local/tmp/pdnr/bin/pd.conf
UHOME=$(grep "^$PD_USER:" "$R/etc/passwd" | cut -d: -f6)
[ -n "$UHOME" ] || { echo "pdn-rustdesk: user $PD_USER not found in Ubuntu" >&2; exit 1; }

rd() { # rustdesk CLI as Ubuntu's root (talks to the running server over IPC)
  sh $PD/bin/pdn-proot.sh /usr/bin/env DISPLAY=":$PD_DISPLAY" /usr/bin/rustdesk "$@"
}
server_up() {
  for p in $(ls -l /proc/[0-9]*/exe 2>/dev/null | grep " -> $PD/tmp/prooted-" | sed 's|.* /proc/\([0-9]*\)/exe .*|\1|'); do
    tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q 'rustdesk --server' && return 0
  done
  return 1
}
# Server running: --option goes over IPC into the live config. Not running: write
# [options] into both config files, so the first start already uses them.
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
    setopt direct-access-port "$PD_RDPORT"
    setopt verification-method use-permanent-password
    setopt approve-mode password
    echo "rustdesk mode: $mode (direct IP port $PD_RDPORT)"
    ;;
  password)
    pw=$(cat)
    [ -n "$pw" ] || { echo "empty password" >&2; exit 1; }
    out=$(rd --password "$pw" 2>&1); pw=
    echo "$out" | grep -qi done && echo "password set" || { echo "rustdesk: $out" >&2; exit 1; }
    ;;
  id)
    rd --get-id 2>/dev/null | tail -n1
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac

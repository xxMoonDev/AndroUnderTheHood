#!/system/bin/sh
# Desktop / RustDesk status and the addresses to connect to (shell user, no root).
. /data/local/tmp/pdnr/bin/pd.conf
[ -x "$R/usr/local/sbin/pd-desktop" ] || { echo "not installed"; exit 1; }
sh $PD/bin/pdn-proot.sh /usr/bin/env PD_USER="$PD_USER" PD_UID="$PD_UID" PD_DISPLAY="$PD_DISPLAY" \
  /usr/local/sbin/pd-desktop status
port=$(printf '%04X' "$PD_RDPORT")
listen=no
grep -qi ":$port " /proc/net/tcp /proc/net/tcp6 2>/dev/null && listen=yes
echo "  RustDesk direct IP port $PD_RDPORT listening: $listen"
echo "  RustDesk ID: $(sh $PD/bin/pdn-rustdesk.sh id 2>/dev/null)"
echo "  Device addresses:"
ip -4 -o addr show 2>/dev/null | while read n dev x addr rest; do
  [ "$dev" = lo ] || echo "    ${addr%%/*}  ($dev)"
done

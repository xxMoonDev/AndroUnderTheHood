#!/system/bin/sh
# Show desktop / RustDesk status and the addresses to connect to.
. "${0%/*}/pd.conf"
[ -x "$R/usr/local/sbin/pd-desktop" ] || { echo "not installed"; exit 1; }
sh $PD/bin/pd-mount.sh || exit 1
DESKENV="PD_MODE=$PD_MODE PD_USER=$PD_USER PD_UID=$PD_UID PD_RES=$PD_RES PD_DISPLAY=$PD_DISPLAY"
if [ $PD_MODE = root ]; then
  sh $PD/bin/pd-shell.sh /usr/bin/env $DESKENV /usr/local/sbin/pd-desktop status
else
  echo "  mode: no root (proot) in $PD"
  SUPER=$(pd_super)
  if [ -n "$SUPER" ]; then
    pd_in_user /usr/bin/env -i $DESKENV PD_SUPER=$SUPER PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
      /usr/local/sbin/pd-desktop status
  else
    echo "  [down] desktop (not started)"
  fi
fi
listen=no
grep -qi ':527E ' /proc/net/tcp /proc/net/tcp6 2>/dev/null && listen=yes
echo "  RustDesk direct IP port 21118 listening: $listen"
echo "  RustDesk ID: $(sh $PD/bin/pd-rustdesk.sh id 2>/dev/null)"
echo "  Device addresses:"
ip -4 -o addr show 2>/dev/null | while read n dev x addr rest; do
  [ "$dev" = lo ] || echo "    ${addr%%/*}  ($dev)"
done

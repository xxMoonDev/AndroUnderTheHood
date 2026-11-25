#!/system/bin/sh
# Start Xvfb + KDE Plasma + RustDesk inside Ubuntu.
#   root mode (su -mm): in the chroot.
#   proot mode (adb shell user): under one long-running proot (the "supervisor", pid in
#   $PD/desktop.pid) that traces every desktop process; pd-stop.sh stops exactly those.
. "${0%/*}/pd.conf"
DESKENV="PD_MODE=$PD_MODE PD_USER=$PD_USER PD_UID=$PD_UID PD_RES=$PD_RES PD_DISPLAY=$PD_DISPLAY"

if [ "$1" = --super ]; then           # (proot mode, internal) become the supervisor
  echo $$ > $PD/desktop.pid
  pd_proot_env
  exec $PD/bin/proot $PROOT_ARGS /usr/bin/env -i $DESKENV HOME=/home/$PD_USER \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/local/sbin/pd-desktop start
fi
if [ "$1" = --sysd ]; then            # (proot mode, internal) fake-root service starter, see pd-sysd.sh
  echo $$ > $PD/sysd.pid
  pd_proot_env
  exec $PD/bin/proot -0 $PROOT_ARGS /usr/bin/env -i PD_USER=$PD_USER     PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/local/sbin/pd-sysd
fi

sh $PD/bin/pd-mount.sh || exit 1
if [ $PD_MODE = root ]; then
  sh $PD/bin/pd-shell.sh /usr/bin/env $DESKENV /usr/local/sbin/pd-desktop start || exit $?
else
  # starts PackageKit/polkit for the desktop's system bus (Discover), as fake root; before
  # the desktop, so Plasma's polkit agent finds polkit at login
  if [ -x "$R/usr/local/sbin/pd-sysd" ] && [ -z "$(pd_sysd)" ]; then
    rm -f $PD/sysd.pid
    setsid sh $PD/bin/pd-start.sh --sysd </dev/null >$PD/sysd.log 2>&1 &
  fi
  if [ -n "$(pd_super)" ]; then
    echo "desktop already running (supervisor $(pd_super))"
  else
    rm -f $PD/desktop.pid
    # own session: survives the adb connection closing
    setsid sh $PD/bin/pd-start.sh --super </dev/null >$PD/desktop.log 2>&1 &
    ok=
    for i in $(seq 120); do
      grep -q '^started:' $PD/desktop.log 2>/dev/null && { ok=1; break; }
      [ $i -gt 3 ] && [ -z "$(pd_super)" ] && break
      sleep 1
    done
    grep -v '^dbus\[' $PD/desktop.log 2>/dev/null | tail -n 5
    [ -n "$ok" ] || { echo "the desktop did not start - log: $PD/desktop.log"; exit 1; }
  fi
fi
# phone <-> Ubuntu bridge, when switched on (pd-bridge.sh config autostart=1)
if grep -q '^BRIDGE_AUTOSTART=1' $PD/bridge.conf 2>/dev/null && [ -f $PD/bin/pd-bridge.sh ]; then
  sh $PD/bin/pd-bridge.sh start
fi
exit 0

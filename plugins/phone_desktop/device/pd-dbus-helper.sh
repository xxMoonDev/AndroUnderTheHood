#!/bin/bash
# proot mode: activation helper of the desktop's system D-Bus (<servicehelper> in the bus
# config pd-desktop writes). Called as: pd-dbus-helper BUS.NAME
# Hands the request to pd-sysd (fake root, see pd-sysd.sh) and waits until the service has
# taken its name on the bus, so D-Bus knows the start worked.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
n=$1
/usr/local/sbin/pd-sysd allowed "$n" || exit 1        # not started in this setup
Q=/run/phonedesktop/sysd
[ -d $Q ] || { echo "pd-dbus-helper: pd-sysd is not running" >&2; exit 1; }
printf '%s' "$n" > $Q/req.$$
for i in $(seq 40); do
  if gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
       --method org.freedesktop.DBus.NameHasOwner "$n" 2>/dev/null | grep -q true; then
    exit 0
  fi
  sleep 0.5
done
rm -f $Q/req.$$
echo "pd-dbus-helper: $n did not start" >&2
exit 1

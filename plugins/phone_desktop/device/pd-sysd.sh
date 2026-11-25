#!/bin/bash
# proot mode only. Runs INSIDE Ubuntu as fake root (proot -0), started by pd-start.sh next to
# the desktop.  pd-sysd           service starter loop
#                pd-sysd allowed NAME   exit 0 if NAME may be started
#
# Why: the desktop's system D-Bus (started by pd-desktop, real uid) can't start root services:
# D-Bus normally uses the setuid dbus-daemon-launch-helper, and nothing is setuid without root.
# Its activation helper (pd-dbus-helper) leaves a request file in $Q instead, and this loop
# starts the service here, where apt/dpkg see root. The bus itself has to stay on the desktop
# side: libdbus clients (Discover's PackageKit library) send their uid when they log in, and a
# bus under fake root sees them as uid 0, not 2000, and drops them.
# Only the services Discover needs are started (no udisks/upower/NetworkManager here).
ALLOW=" org.freedesktop.PackageKit org.freedesktop.PolicyKit1 "
if [ "$1" = allowed ]; then case "$ALLOW" in *" $2 "*) exit 0 ;; *) exit 1 ;; esac; fi

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8
PD_USER=${PD_USER:-kde}
Q=/run/phonedesktop/sysd
LOG=/var/log/phonedesktop; mkdir -p $LOG $Q
exec >>$LOG/sysd.log 2>&1
echo "pd-sysd started $(date)"
rm -f $Q/req.*

# (the polkit rule that lets the desktop user install without a password: pd-desktop writes it)

# polkit only lets root, or an action's declared owner, ask about another process
# (CheckAuthorization with details, which PackageKit always sends). PackageKit runs here as
# fake root, but the bus sees its real uid, the desktop user's. So the desktop user is made
# owner of PackageKit's actions: the packaged file is diverted (package updates go to
# $PKP.pd-orig) and the copy polkit reads gets an owner line per action.
PKP=/usr/share/polkit-1/actions/org.freedesktop.packagekit.policy
if [ -f $PKP ] || [ -f $PKP.pd-orig ]; then
  dpkg-divert --list $PKP | grep -q . ||
    dpkg-divert --local --rename --divert $PKP.pd-orig --add $PKP >/dev/null
  sed "s#^\( *\)</action>#\1  <annotate key=\"org.freedesktop.policykit.owner\">unix-user:$PD_USER</annotate>\n\1</action>#" \
    $PKP.pd-orig > $PKP.new && mv -f $PKP.new $PKP
fi

service_exec() { # bus name -> Exec line of its system service file
  local d f
  for d in /usr/local/share/dbus-1/system-services /usr/share/dbus-1/system-services; do
    for f in "$d"/*.service; do
      [ -f "$f" ] && grep -qx "Name=$1" "$f" && { sed -n 's/^Exec=//p' "$f" | head -n1; return; }
    done
  done
}

# a pipe nobody writes to: `read -t 1` on it is a sleep without starting a process each second
exec 3<> <(:)
while :; do
  for r in $Q/req.*; do
    [ -f "$r" ] || continue
    n=$(head -c 200 "$r"); rm -f "$r"
    case "$ALLOW" in *" $n "*) ;; *) echo "not allowed: $n"; continue ;; esac
    ex=$(service_exec "$n")
    [ -n "$ex" ] || { echo "no service file for $n"; continue; }
    echo "$(date +%T) starting $n: $ex"
    setsid $ex </dev/null >>"$LOG/sysd-${n##*.}.log" 2>&1 &
  done
  read -t 1 -u 3 || true
done

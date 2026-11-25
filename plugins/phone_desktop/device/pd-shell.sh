#!/system/bin/sh
# Enter Ubuntu:  pd-shell.sh [command ...]
#   root mode (su -mm): chroot, as root.   proot mode (adb shell user): proot, as fake root.
# With no command an interactive root login shell is started.
. "${0%/*}/pd.conf"
sh $PD/bin/pd-mount.sh || exit 1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root TERM=${TERM:-xterm-256color} LANG=C.UTF-8
unset LD_PRELOAD ANDROID_ROOT ANDROID_DATA BOOTCLASSPATH TMPDIR
if [ $# -eq 0 ]; then
  pd_in /bin/bash -l
else
  pd_in "$@"
fi

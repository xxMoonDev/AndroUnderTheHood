#!/system/bin/sh
# Magisk service.d hook (installed only with deploy.bat /autostart):
# starts the PhoneDesktop chroot desktop once Android has finished booting.
until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 5; done
sleep 20   # let Wi-Fi / VPN come up
sh /data/local/phonedesktop/bin/pd-start.sh > /data/local/phonedesktop/boot.log 2>&1

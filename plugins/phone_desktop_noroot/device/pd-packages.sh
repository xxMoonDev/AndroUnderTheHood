# Package sets, sourced (inside the chroot) by pd-chroot-setup and pd-bundle.
PD_BASE_PKGS="locales tzdata sudo dbus dbus-x11 ca-certificates curl procps psmisc
  xvfb xauth x11-xserver-utils x11-utils xdotool
  pulseaudio fonts-noto-core fonts-noto-color-emoji
  gcc libc6-dev libxcb-shm0-dev"

pd_kde_pkgs() { # profile -> KDE package list
  case "$1" in
    minimal) echo "plasma-desktop konsole dolphin kate systemsettings plasma-workspace breeze" ;;
    full)    echo "kde-full plasma-workspace breeze" ;;
    *)       echo "kde-standard plasma-workspace breeze" ;;
  esac
}

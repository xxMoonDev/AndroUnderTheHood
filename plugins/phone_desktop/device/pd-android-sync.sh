#!/bin/bash
# Installed in Ubuntu as /usr/local/bin/android-apps-sync (PhoneDesktop "Android apps").
# Reads this phone's launcher apps (name + icon) over SSH and keeps one Plasma start-menu entry per app
# in an "Android" submenu; entries of removed apps are deleted. Run by android-apps-watch on changes.
#   android-apps-sync [--icons]     --icons: fetch every icon again (after an app changed its icon)
set -u
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/android-apps/config
[ -f "$CONF" ] || { echo "android-apps-sync: not set up ($CONF missing)" >&2; exit 1; }
. "$CONF"
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
APPS=$DATA/applications                       # entries: android-app-<package>.desktop
ICONS=$DATA/phonedesktop/android-icons        # <package>.png, <package>.noicon (icon crashes the helper)
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/android-apps
mkdir -p "$APPS" "$ICONS" "$CACHE" "$DATA/desktop-directories" "${XDG_CONFIG_HOME:-$HOME/.config}/menus/applications-merged"
JAR_LOCAL=/usr/local/share/phonedesktop/pd-android-apps.jar
JAR=/data/local/tmp/pd-android-apps.jar
[ "${1:-}" = --icons ] && rm -f "$ICONS"/*.png "$ICONS"/*.noicon

# user@host:port -> ssh arguments
user=${SSH_TARGET%%@*}; hp=${SSH_TARGET#*@}; host=${hp%%:*}; port=${hp##*:}
[ "$port" = "$hp" ] && port=22
SSH=(ssh -T -i "$SSH_KEY" -o IdentitiesOnly=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" -o StrictHostKeyChecking=yes
     -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR -p "$port")
[ "${SSH_ROOT:-0}" = 1 ] && SSH+=(-o SetEnv=ROOTSSH_ROOT=1)   # root session in AndroOpenHeart
SSH+=("$user@$host")

# the device-side helper: pushed once, again when it changed
want=$(md5sum < "$JAR_LOCAL" | cut -c1-32)
have=$("${SSH[@]}" "md5sum $JAR 2>/dev/null" | cut -c1-32) || { echo "android-apps-sync: SSH to $SSH_TARGET failed" >&2; exit 2; }
if [ "$want" != "$have" ]; then
  "${SSH[@]}" "cat > $JAR.part && chmod 644 $JAR.part && mv $JAR.part $JAR" < "$JAR_LOCAL" || exit 2
fi
helper() { "${SSH[@]}" "CLASSPATH=$JAR app_process / PdAndroidApps $*" 2>/dev/null; }

# ---- the launcher list: A <pkg> <activity> <label>
list=$(helper list) || true
echo "$list" | grep -q '^N	' || { echo "android-apps-sync: the app list could not be read" >&2; exit 3; }
declare -A LABEL KEY
order=()
while IFS=$'\t' read -r t pkg cls label; do
  [ "$t" = A ] || continue
  [[ $pkg =~ ^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$ ]] || continue
  [ -n "${LABEL[$pkg]+x}" ] && continue            # one entry per package (scrcpy starts the package)
  LABEL[$pkg]=$label; KEY[$pkg]="$pkg/$cls"; order+=("$pkg")
done <<< "$list"

# ---- icons, only the missing ones. Some icons abort the helper (they draw text); it prints S<key>
# before each one, so we restart after the last good one and remember the bad one (.noicon).
need=()
for pkg in "${order[@]}"; do
  [ -f "$ICONS/$pkg.png" ] || [ -f "$ICONS/$pkg.noicon" ] || need+=("${KEY[$pkg]}")
done
tries=0
while [ ${#need[@]} -gt 0 ] && [ $tries -lt 20 ]; do
  tries=$((tries + 1))
  out=$(helper icons 96 "${need[@]}") || true
  started=""
  while IFS=$'\t' read -r t key b64; do
    case "$t" in
      S) started=$key ;;
      I) started=""; p=${key%%/*}
         if [ -n "$b64" ] && echo "$b64" | base64 -d > "$ICONS/$p.png.part" 2>/dev/null; then
           mv "$ICONS/$p.png.part" "$ICONS/$p.png"
         else rm -f "$ICONS/$p.png.part"; touch "$ICONS/$p.noicon"; fi ;;
    esac
  done <<< "$out"
  [ -n "$started" ] && touch "$ICONS/${started%%/*}.noicon"     # this one crashed the helper
  rest=()
  for k in "${need[@]}"; do p=${k%%/*}; [ -f "$ICONS/$p.png" ] || [ -f "$ICONS/$p.noicon" ] || rest+=("$k"); done
  [ ${#rest[@]} -eq ${#need[@]} ] && break                          # no progress: give up for now
  need=("${rest[@]}")
done

# ---- menu entries
esc() { printf '%s' "$1" | tr -d '\r\n' | sed 's/\\/\\\\/g'; }
n=0
declare -A KEEP
for pkg in "${order[@]}"; do
  f="$APPS/android-app-$pkg.desktop"; KEEP[$f]=1
  icon=smartphone; [ -f "$ICONS/$pkg.png" ] && icon="$ICONS/$pkg.png"
  new="[Desktop Entry]
Type=Application
Name=$(esc "${LABEL[$pkg]}")
Comment=Android app on this phone ($pkg)
Exec=/usr/local/bin/android-app $pkg \"$(esc "${LABEL[$pkg]}" | sed 's/["`$%\\]//g')\"
Icon=$icon
Categories=X-PhoneDesktop-Android;
StartupWMClass=$pkg
X-PhoneDesktop-Package=$pkg"
  [ "$(cat "$f" 2>/dev/null)" = "$new" ] || printf '%s\n' "$new" > "$f"
  n=$((n + 1))
done
# whole phone screen
f="$APPS/android-app--screen.desktop"; KEEP[$f]=1
printf '%s\n' "[Desktop Entry]" "Type=Application" "Name=Phone screen" "Comment=Mirror the phone's whole screen" \
  "Exec=/usr/local/bin/android-app --screen" "Icon=smartphone" "Categories=X-PhoneDesktop-Android;" > "$f"
# removed apps
for f in "$APPS"/android-app-*.desktop; do
  [ -e "$f" ] || continue
  [ -n "${KEEP[$f]+x}" ] && continue
  p=$(sed -n 's/^X-PhoneDesktop-Package=//p' "$f"); rm -f "$f" "$ICONS/$p.png" "$ICONS/$p.noicon"
done

# the "Android" submenu
cat > "$DATA/desktop-directories/phonedesktop-android.directory" <<'EOF'
[Desktop Entry]
Type=Directory
Name=Android
Comment=Apps of this phone, opened through scrcpy
Icon=smartphone
EOF
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/menus/applications-merged/phonedesktop-android.menu" <<'EOF'
<!DOCTYPE Menu PUBLIC "-//freedesktop//DTD Menu 1.0//EN" "http://www.freedesktop.org/standards/menu-spec/menu-1.0.dtd">
<Menu>
  <Name>Applications</Name>
  <Menu>
    <Name>Android</Name>
    <Directory>phonedesktop-android.directory</Directory>
    <Include><Category>X-PhoneDesktop-Android</Category></Include>
  </Menu>
</Menu>
EOF
# let Plasma pick the changes up now
for k in kbuildsycoca6 kbuildsycoca5; do command -v $k >/dev/null && { $k >/dev/null 2>&1; break; }; done
echo "android-apps-sync: $n apps in the Android menu ($(ls "$ICONS"/*.png 2>/dev/null | wc -l) icons)"

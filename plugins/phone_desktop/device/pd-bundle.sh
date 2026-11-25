#!/bin/bash
# Runs INSIDE the chroot as root (needs internet):  pd-bundle <profile>
# Downloads every .deb a FRESH install needs into /var/cache/phonedesktop/bundle
# and turns it into a flat apt repository (Packages + Release) for offline
# deploys. Resolution uses an EMPTY dpkg status, so apt fetches the complete
# dependency closure, not just what this device is missing.
set -e
PROFILE=${1:-standard}
. /usr/local/sbin/pd-packages
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
OUT=/var/cache/phonedesktop/bundle

echo "-- apt update"
apt-get update
dpkg -s apt-utils >/dev/null 2>&1 || apt-get install -y --no-install-recommends apt-utils

# what to fetch: the profile's packages, plus everything installed on this
# device (covers the Ubuntu base system so offline full-upgrade stays consistent)
DEB=$(ls /var/cache/phonedesktop/rustdesk-*-aarch64.deb | head -n1)
LIST="$PD_BASE_PKGS $(pd_kde_pkgs "$PROFILE")"
# rustdesk + elixiroffice come as local .debs (cache), not from the bundle
LIST="$LIST $(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' | awk '$1=="ii" && $2!="rustdesk" && $2!="elixiroffice-desktop" {print $2}')"

echo "-- downloading the full dependency closure (profile $PROFILE)"
rm -rf "$OUT"; mkdir -p "$OUT/partial"
: > /tmp/pd-empty-status
apt-get -o Dir::State::status=/tmp/pd-empty-status -o Dir::Cache::archives="$OUT" \
  -o APT::Sandbox::User=root -o Debug::NoLocking=1 \
  install -y --download-only $LIST "$DEB"
rm -rf "$OUT/partial" "$OUT/lock" "$OUT"/rustdesk_*.deb

echo "-- building repository index"
cd "$OUT"
apt-ftparchive packages . > Packages
apt-ftparchive release . > Release
echo "$PROFILE" > PROFILE
date -u +%Y-%m-%dT%H:%MZ > BUILT
echo "-- bundle: $(ls *.deb | wc -l) packages, $(du -sh . | cut -f1)"

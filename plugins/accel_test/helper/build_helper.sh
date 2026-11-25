#!/bin/sh
# Rebuilds acctest.jar (dex) from AccTest.java. Needs JDK 11+ and the Android SDK.
set -e
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-$LOCALAPPDATA/Android/Sdk}"
rm -rf classes && mkdir classes
javac --release 11 -nowarn -cp "$SDK/platforms/android-37.0/android.jar" -d classes AccTest.java
"$SDK/build-tools/36.0.0/d8.bat" --release --min-api 21 --lib "$SDK/platforms/android-37.0/android.jar" --output ../acctest.jar classes/*.class
rm -rf classes

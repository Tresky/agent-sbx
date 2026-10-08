# Android: a JDK, the SDK command-line tools, a platform and build tools; the emulator on request
#
# Settings, in a definition's [android] table:
#   platform     the SDK platform (default 35): platforms;android-<platform>
#   build_tools  the build tools (default 35.0.0)
#   emulator     true: the emulator, a system image and an emulator device
#                named Medium_Phone (about 3 GB more; default false). It
#                runs without a window, with KVM: the sandbox has /dev/kvm
#                when the host allows nested virtualization.
#   image        the system image, after its platform (default google_apis;x86_64)
#
# The SDK is /opt/android-sdk, owned by the sandbox user, so Gradle can add a
# package that a project asks for. ANDROID_HOME is set for every shell.
: "${SBX_ANDROID_PLATFORM:=35}"
: "${SBX_ANDROID_BUILD_TOOLS:=35.0.0}"
: "${SBX_ANDROID_EMULATOR:=0}"
: "${SBX_ANDROID_IMAGE:=google_apis;x86_64}"
SDK=/opt/android-sdk

step "android sdk: platform $SBX_ANDROID_PLATFORM, build-tools $SBX_ANDROID_BUILD_TOOLS$([[ "$SBX_ANDROID_EMULATOR" == 1 ]] && echo ", emulator")"
# Debian 13 has no JDK 17; 21 builds a Java 17 target with Gradle 8.5 and later.
apt-get install -y -q --no-install-recommends openjdk-21-jdk-headless unzip

# The newest command-line tools, by name from Google's repository index.
clt_url="$(python3 - <<'PY'
import urllib.request, xml.etree.ElementTree as ET
base = "https://dl.google.com/android/repository/"
root = ET.fromstring(urllib.request.urlopen(base + "repository2-3.xml", timeout=60).read())
for pkg in root.iter("remotePackage"):
    if pkg.get("path") == "cmdline-tools;latest":
        for archive in pkg.iter("archive"):
            if (archive.findtext("host-os") or "") == "linux":
                print(base + archive.find("complete").findtext("url"))
                raise SystemExit
raise SystemExit("no cmdline-tools;latest for linux in the repository index")
PY
)"
tmp="$(mktemp -d)"
curl -fsSL --retry 5 --retry-delay 5 "$clt_url" -o "$tmp/clt.zip"
unzip -q "$tmp/clt.zip" -d "$tmp"
install -d "$SDK/cmdline-tools"
rm -rf "$SDK/cmdline-tools/latest"
mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
rm -rf "$tmp"
chown -R "$U:$U" "$SDK"

android_packages=("platform-tools" "platforms;android-$SBX_ANDROID_PLATFORM" "build-tools;$SBX_ANDROID_BUILD_TOOLS")
if [[ "$SBX_ANDROID_EMULATOR" == 1 ]]; then
  android_packages+=("emulator" "system-images;android-$SBX_ANDROID_PLATFORM;$SBX_ANDROID_IMAGE")
  usermod -aG kvm "$U"
fi
quoted=""
for p in "${android_packages[@]}"; do quoted+=" '$p'"; done
sdkm="$SDK/cmdline-tools/latest/bin/sdkmanager --sdk_root=$SDK"
as_user "yes | $sdkm --licenses >/dev/null 2>&1; $sdkm$quoted"

# Every shell: zsh reads /etc/zsh/zshenv, ssh sessions /etc/environment.
printf 'ANDROID_HOME=%s\nANDROID_SDK_ROOT=%s\n' "$SDK" "$SDK" >> /etc/environment
printf 'export ANDROID_HOME=%s ANDROID_SDK_ROOT=%s\n' "$SDK" "$SDK" >> /etc/zsh/zshenv
ln -sf "$SDK/platform-tools/adb" /usr/local/bin/adb
for tool in sdkmanager avdmanager; do
  ln -sf "$SDK/cmdline-tools/latest/bin/$tool" "/usr/local/bin/$tool"
done

if [[ "$SBX_ANDROID_EMULATOR" == 1 ]]; then
  ln -sf "$SDK/emulator/emulator" /usr/local/bin/emulator
  # The device that a project's instructions most often name.
  as_user "echo no | ANDROID_HOME=$SDK $SDK/cmdline-tools/latest/bin/avdmanager create avd -n Medium_Phone \
    -k 'system-images;android-$SBX_ANDROID_PLATFORM;$SBX_ANDROID_IMAGE' -d medium_phone \
    || echo 'WARNING: no emulator device made; avdmanager create avd by hand'"
fi
CHECK_TOOLS+=" adb sdkmanager java"

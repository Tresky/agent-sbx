# Odin, with wgpu-native, premake, SDL2, Lua and software Vulkan
#
# Settings, in a definition's [odin] table:
#   version          the Odin release (default dev-2025-11)
#   wgpu_version     the wgpu-native release (default v27.0.2.0)
#   premake_version  the premake release (default 5.0.0-beta8)
: "${SBX_ODIN_VERSION:=dev-2025-11}"
: "${SBX_ODIN_WGPU_VERSION:=v27.0.2.0}"
: "${SBX_ODIN_PREMAKE_VERSION:=5.0.0-beta8}"

step "odin apt packages"
apt-get install -y -q --no-install-recommends libsdl2-dev liblua5.4-dev \
  mesa-vulkan-drivers vulkan-tools libvulkan1

step "odin toolchain ($SBX_ODIN_VERSION, wgpu $SBX_ODIN_WGPU_VERSION)"
# The release layout has changed shape across versions, so the binary is
# located.
tmp="$(mktemp -d)"
wget -q --tries=5 --waitretry=5 --retry-connrefused "https://github.com/odin-lang/Odin/releases/download/${SBX_ODIN_VERSION}/odin-linux-amd64-${SBX_ODIN_VERSION}.zip" -O "$tmp/odin.zip"
unzip -q "$tmp/odin.zip" -d "$tmp/x"
for t in "$tmp"/x/*.tar.gz; do [[ -e "$t" ]] && tar -xzf "$t" -C "$tmp/x"; done
odin_bin="$(find "$tmp/x" -type f -name odin | head -1)"
[[ -n "$odin_bin" ]]
rm -rf /opt/odin
mv "$(dirname "$odin_bin")" /opt/odin
chmod +x /opt/odin/odin
# stb and miniaudio ship prebuilt for darwin/wasm/windows only.
make -C /opt/odin/vendor/stb/src
make -C /opt/odin/vendor/miniaudio/src
wgpu_dir="/opt/odin/vendor/wgpu/lib/wgpu-linux-x86_64-release"
wget -q --tries=5 --waitretry=5 --retry-connrefused "https://github.com/gfx-rs/wgpu-native/releases/download/${SBX_ODIN_WGPU_VERSION}/wgpu-linux-x86_64-release.zip" -O "$tmp/wgpu.zip"
mkdir -p "$wgpu_dir"
unzip -q -o "$tmp/wgpu.zip" -d "$wgpu_dir"
[[ -f "$wgpu_dir/lib/libwgpu_native.a" ]]
# imgui.odin asks for -lc++ (a macOS name); g++ built the objects against
# libstdc++, so the alias names the runtime they actually reference.
triplet="$(gcc -print-multiarch)"
ln -sf "/usr/lib/$triplet/libstdc++.so.6" "/usr/lib/$triplet/libc++.so"
wget -q --tries=5 --waitretry=5 --retry-connrefused "https://github.com/premake/premake-core/releases/download/v${SBX_ODIN_PREMAKE_VERSION}/premake-${SBX_ODIN_PREMAKE_VERSION}-linux.tar.gz" -O "$tmp/premake.tgz"
tar -xzf "$tmp/premake.tgz" -C /usr/local/bin premake5
chmod +x /usr/local/bin/premake5
rm -rf "$tmp"
chown -R root:root /opt/odin
/opt/odin/odin version
CHECK_TOOLS+=" odin"

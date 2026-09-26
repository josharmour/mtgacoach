#!/bin/zsh
# Build the Android arm64 probe from the shared source (../mac-il2cpp/probe.cpp).
set -euo pipefail
here=${0:A:h}
ndk=${ANDROID_NDK_HOME:-$(ls -d /opt/homebrew/share/android-commandlinetools/ndk/* | sort -V | tail -1)}
clang=$(ls "$ndk"/toolchains/llvm/prebuilt/*/bin/aarch64-linux-android29-clang++ | head -1)
mkdir -p "$here/build"
"$clang" -std=c++17 -O1 -g -Wall -Wextra -Wno-unused-parameter \
    -shared -fPIC -fvisibility=hidden -static-libstdc++ -llog \
    -Wl,-soname,libmtgacoach_probe.so \
    -o "$here/build/libmtgacoach_probe.so" "$here/../mac-il2cpp/probe.cpp"
echo "built: $here/build/libmtgacoach_probe.so"

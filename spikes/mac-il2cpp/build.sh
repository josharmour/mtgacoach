#!/bin/zsh
# Build the universal (arm64 + x86_64) probe dylib and stage it on local disk,
# where it is injected from (~/.arenamcp/probe/).
set -euo pipefail
here=${0:A:h}
mkdir -p "$here/build" "$HOME/.arenamcp/probe"
clang++ -std=c++17 -O1 -g -Wall -Wextra -Wno-unused-parameter \
    -shared -fPIC -fvisibility=hidden -arch arm64 -arch x86_64 \
    -o "$here/build/libmtgacoach_probe.dylib" "$here/probe.cpp"
# Install by rename, never in place: a running MTGA keeps the old inode mapped,
# and rewriting a mapped, signed library under it can get the game killed.
staged="$HOME/.arenamcp/probe/.libmtgacoach_probe.dylib.$$"
cp "$here/build/libmtgacoach_probe.dylib" "$staged"
mv -f "$staged" "$HOME/.arenamcp/probe/libmtgacoach_probe.dylib"
echo "built and staged: $HOME/.arenamcp/probe/libmtgacoach_probe.dylib"

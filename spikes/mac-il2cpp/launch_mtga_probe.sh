#!/bin/zsh
# Launch the native Mac MTGA client with the IL2CPP probe injected.
# MTGA must not already be running. Steam must be running (SteamAppId lets the
# game initialise Steamworks without relaunching itself through Steam).
set -euo pipefail
dylib="$HOME/.arenamcp/probe/libmtgacoach_probe.dylib"
app="$HOME/Library/Application Support/Steam/steamapps/common/MTGA/MTGA.app"
[[ -f $dylib ]] || { echo "build first: ${0:A:h}/build.sh"; exit 1; }
if pgrep -f "$app/Contents/MacOS/MTGA" >/dev/null; then
    echo "MTGA is running; quit it first"
    exit 1
fi
cd "$app/.."
# Exec MTGA directly: routing through a SIP-protected binary such as
# /usr/bin/nohup or /usr/bin/env strips DYLD_* from the child's environment.
SteamAppId=2141910 SteamGameId=2141910 DYLD_INSERT_LIBRARIES="$dylib" \
    "$app/Contents/MacOS/MTGA" </dev/null >/dev/null 2>&1 &!
echo "launched MTGA; probe log: ~/.arenamcp/il2cpp_probe.log"

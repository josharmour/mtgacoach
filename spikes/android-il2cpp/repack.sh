#!/bin/zsh
# Unrooted-phone setup for the mtgacoach probe: repack MTGA's own APKs with the
# probe added as a DT_NEEDED of lib/arm64-v8a/libmain.so, re-sign with a local
# key, and sideload over adb (USB or wireless). One command does everything:
#
#   repack.sh              pull + patch + install the connected phone
#   repack.sh pull         pull base + split APKs from the phone (Play build)
#   repack.sh patch        inject probe + DT_NEEDED into the arm64 split, align + sign
#   repack.sh install      adb install-multiple the signed APKs (uninstalls Play build once)
#   repack.sh status       probe logcat + installed lib check on the phone
#   repack.sh clean        remove intermediates (keeps the keystore)
#
# Requirements here: adb, zip/unzip, patchelf, zipalign, apksigner, keytool.
#   macOS: brew install patchelf; zipalign/apksigner come from Android
#   build-tools (this script adds the usual commandlinetools/SDK dirs to PATH).
# On the phone: nothing special — USB debugging (or an already-paired wireless
# debugging connection). No root.
#
# Trade-offs of re-signing (inherent to unrooted injection):
#   - The Play build must be uninstalled once (account is server-side; local
#     settings/log history reset). The script does this automatically on
#     signature mismatch.
#   - Play updates no longer auto-apply: re-run `repack.sh` after each MTGA
#     update (a Play update also silently removes the probe).
#   - Root-only niceties skipped: bridge-port firewall rule, tcp_mtu_probing.
#
# Redistribution note: this never downloads MTGA; it repacks the copy already
# on the user's own device for their own use.
set -euo pipefail

here=${0:A:h}
pkg=com.wizards.mtga
probe=libmtgacoach_probe.so
work="$here/repack"
state="$HOME/.arenamcp/android-probe"
keystore="$state/repack.keystore"
ks_alias=mtgacoach
bridge_port=${MTGACOACH_ANDROID_BRIDGE_PORT:-44222}
script_name=repack.sh

need() { command -v "$1" >/dev/null || { echo "missing tool: $1" >&2; exit 1; }; }
for t in adb zip unzip patchelf zipalign apksigner keytool; do need "$t"; done

# build-tools ship zipalign/apksigner outside PATH on macOS; add them if found.
setopt null_glob
for bt in /opt/homebrew/share/android-commandlinetools/build-tools/*/ \
          "$HOME"/Library/Android/sdk/build-tools/*/; do
    [[ -d $bt ]] && export PATH="${bt%%/}:$PATH"
done

pick_device() {
    # Explicit override wins; else prefer USB (serials without ':' / 'adb-').
    if [[ -n ${MTGACOACH_ADB_SERIAL:-} ]]; then echo "$MTGACOACH_ADB_SERIAL"; return; fi
    local line serial usb=""
    for line in $(adb devices | tail -n +2); do
        serial=${line%%$'\t'*}
        [[ $line == *"	device" ]] || continue
        if [[ $serial != *:* && $serial != adb-* ]]; then usb=$serial; break; fi
        [[ -z $usb ]] && usb=$serial   # wireless fallback
    done
    [[ -n $usb ]] || { echo "no connected phone (adb devices is empty)" >&2; exit 1; }
    echo "$usb"
}

ADB() { adb -s "$SERIAL" "$@"; }

case ${1:-all} in
    pull|patch|install|all|status) SERIAL=$(pick_device) ;;
esac

ensure_keystore() {
    mkdir -p "$state"
    [[ -f $keystore ]] && return
    keytool -genkeypair -keystore "$keystore" -alias "$ks_alias" \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass mtgacoach -keypass mtgacoach \
        -dname "CN=mtgacoach repack" >/dev/null 2>&1
    echo "keystore created: $keystore"
}

pull_apks() {
    local paths
    paths=$(ADB shell pm path $pkg | tr -d '\r' | sed 's/^package://' | grep '\.apk$')
    [[ -n $paths ]] || { echo "MTGA ($pkg) is not installed on $SERIAL" >&2; exit 1; }
    rm -rf "$work/apks"; mkdir -p "$work/apks"
    local i=0 p
    for p in ${(f)paths}; do
        i=$((i+1))
        adb pull -q "$p" "$work/apks/$(printf '%02d' $i)-${p:t}"
    done
    ls -la "$work/apks"
}

# The split containing lib/arm64-v8a/libmain.so (usually config.arm64_v8a.apk).
arm64_split() {
    local f
    for f in "$work"/apks/*.apk(N); do
        # Redirect to a file before grepping: `unzip | grep -q` can die with
        # SIGPIPE under pipefail when grep exits early on a big listing.
        unzip -l "$f" > /tmp/mtgacoach-unzip.out 2>/dev/null || true
        if grep -q "lib/arm64-v8a/libmain.so" /tmp/mtgacoach-unzip.out; then
            echo "$f"
            return
        fi
    done
    echo "no split contains lib/arm64-v8a/libmain.so; is this an arm64 device build?" >&2
    exit 1
}

patch_apks() {
    [[ -f $here/build/$probe ]] || "$here/build.sh"
    local split; split=$(arm64_split)
    local split_name=${split:t}
    echo "patching split: $split_name"

    rm -rf "$work/extract"; mkdir -p "$work/extract"
    unzip -q "$split" -d "$work/extract"

    # Strip old signatures; apksigner writes fresh ones.
    find "$work/extract/META-INF" -type f \( -name '*.SF' -o -name '*.RSA' -o -name '*.DSA' -o -name '*.EC' \) -delete 2>/dev/null || true

    local libdir="$work/extract/lib/arm64-v8a"
    cp "$here/build/$probe" "$libdir/$probe"
    if patchelf --print-needed "$libdir/libmain.so" | grep -qx $probe; then
        echo "libmain.so already loads the probe"
    else
        patchelf --add-needed $probe "$libdir/libmain.so"
    fi

    # Repack: non-.so entries deflated, but .so and resources.arsc STORED
    # (uncompressed) — manifests built with extractNativeLibs=false refuse
    # compressed libraries, and API 30+ rejects a compressed resources.arsc.
    local staged_base=${split_name%.apk}
    local staged="$work/$staged_base-patched.apk"
    rm -f "$staged"
    (
        cd "$work/extract"
        zip -q -r "$staged" . -x 'META-INF/*' -x '*.so' -x 'resources.arsc'
        find . \( -name '*.so' -o -name resources.arsc \) -type f | sed 's|^\./||' | while read -r f; do
            zip -q -X -0 "$staged" "$f"
        done
    )
    zipalign -p -f 4 "$staged" "$work/$staged_base-aligned.apk"
    mv "$work/$staged_base-aligned.apk" "$staged"

    ensure_keystore
    # Re-sign every pulled APK with the SAME key so install-multiple takes them together.
    local f out src
    for f in "$work"/apks/*.apk(N); do
        out="$work/signed-${f:t}"
        src="$f"
        if [[ ${f:t} == $split_name ]]; then src="$staged"; out="$work/signed-$split_name"; fi
        rm -f "$out"
        apksigner sign --ks "$keystore" --ks-pass pass:mtgacoach \
            --ks-key-alias "$ks_alias" --out "$out" "$src"
        apksigner verify --print-certs "$out" >/dev/null && echo "signed ${out:t}"
    done
}

install_apks() {
    local apks=("$work"/signed-*.apk(N))
    [[ ${#apks[@]} -ge 1 ]] || { echo "nothing signed yet; run '$script_name patch' first" >&2; exit 1; }
    # Signature differs from Play -> an installed Play build blocks the update.
    if ! adb -s "$SERIAL" install-multiple -r "${apks[@]}" 2>"$work/install.err"; then
        if grep -q INSTALL_FAILED_UPDATE_INCOMPATIBLE "$work/install.err"; then
            echo "signatures differ from the installed Play build."
            echo "Removing it once (account is server-side; local settings reset)…"
            adb -s "$SERIAL" uninstall $pkg >/dev/null || true
            adb -s "$SERIAL" install-multiple "${apks[@]}"
        else
            cat "$work/install.err" >&2; exit 1
        fi
    fi

    # Wire the coach bridge port now so the probe can dial home on first launch.
    adb reverse --remove tcp:$bridge_port 2>/dev/null || true
    if adb reverse tcp:$bridge_port tcp:$bridge_port 2>/dev/null; then
        echo "adb reverse: phone :$bridge_port -> this computer :$bridge_port"
        echo "note (unrooted): adbd accepts that port on every interface while the"
        echo "reverse exists; with root, install.sh also firewalls it to loopback."
    else
        echo "note: could not set adb reverse yet (coach not listening on $bridge_port?)."
        echo "      android_link.py re-creates it when the coach starts."
    fi

    echo "installed on $SERIAL. Start MTGA, then:"
    echo "  adb -s $SERIAL logcat -s mtgacoach     # probe progress"
}

show_status() {
    echo "device: $SERIAL"
    local dir; dir=$(ADB shell pm path $pkg 2>/dev/null | head -1 | sed 's|package:||; s|/base.apk||' | tr -d '\r')
    if [[ -n $dir ]]; then
        ADB shell ls "'$dir/lib/arm64/'" 2>/dev/null | grep -E 'mtgacoach|libmain' || true
        ADB exec-out cat "'$dir/lib/arm64/libmain.so'" > /tmp/libmain.status.so 2>/dev/null || true
        echo "libmain NEEDED: $(patchelf --print-needed /tmp/libmain.status.so 2>/dev/null | tr '\n' ' ')"
    else
        echo "MTGA not installed"
    fi
    echo "--- probe log (logcat)"
    ADB logcat -d 2>/dev/null | grep ' mtgacoach:' | tail -15 || true
}

clean() {
    rm -rf "$work/extract" "$work/apks"
    rm -f "$work"/signed-*.apk(N) "$work"/signed-*.apk.idsig(N) \
          "$work"/*-patched.apk(N) 2>/dev/null || true
}

case ${1:-all} in
    pull) pull_apks ;;
    patch) patch_apks ;;
    install) install_apks ;;
    all) pull_apks && patch_apks && install_apks ;;
    status) show_status ;;
    clean) clean ;;
    *) echo "usage: $script_name [pull|patch|install|all|status|clean]" >&2; exit 2 ;;
esac
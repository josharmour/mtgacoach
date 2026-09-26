#!/bin/zsh
# Load the mtgacoach probe into the Play-installed Android MTGA on a rooted
# (or userdebug) device over adb, and wire its sockets to this computer.
#
#   install.sh install     force-stop MTGA, add libmtgacoach_probe.so as a
#                          DT_NEEDED of the installed libmain.so, set up adb ports
#   install.sh uninstall   restore the original libmain.so, remove the probe
#   install.sh status      show what is installed and the probe's recent log
#   install.sh ports       re-create the adb reverse/forward after a replug
#   install.sh mtu-fix     enable TCP MTU probing (path-MTU black holes on
#                          tunnelled Wi-Fi; resets on reboot)
#
# A Play Store update of MTGA replaces its lib directory, which silently
# uninstalls the probe; run `install` again after each update.
set -euo pipefail
here=${0:A:h}
pkg=com.wizards.mtga
probe=libmtgacoach_probe.so
state=/data/local/tmp/mtgacoach
local_state="$HOME/.arenamcp/android-probe"

su_sh() { adb shell "su 0 sh -c '$1'"; }

lib_dir() {
    local base
    base=$(adb shell pm path $pkg | tr -d '\r' | grep '/base.apk$' | sed 's/^package://')
    [[ -n $base ]] || { echo "MTGA ($pkg) is not installed" >&2; exit 1; }
    echo "${base%/base.apk}/lib/arm64"
}

# The probe always uses 44222/44223 on the phone. The computer-side ports are
# configurable so a Mac already running MTGA + coach (which hold 44222/44223)
# is not disturbed: MTGACOACH_ANDROID_BRIDGE_PORT / MTGACOACH_ANDROID_DIAG_PORT.
bridge_port=${MTGACOACH_ANDROID_BRIDGE_PORT:-44222}
diag_port=${MTGACOACH_ANDROID_DIAG_PORT:-44233}

ports() {
    adb reverse tcp:44222 tcp:$bridge_port >/dev/null   # probe -> coach GRE bridge on this computer
    adb forward tcp:$diag_port tcp:44223 >/dev/null     # this computer -> probe diagnostics
    # adbd listens for the reverse on every interface; only the probe (loopback) may use it.
    su_sh "for t in iptables ip6tables; do \$t -C INPUT -p tcp --dport 44222 ! -i lo -j DROP 2>/dev/null || \$t -I INPUT -p tcp --dport 44222 ! -i lo -j DROP; done"
    echo "adb ports: phone 44222 -> computer $bridge_port (bridge), computer $diag_port -> phone 44223 (diagnostics)"
}

install_probe() {
    "$here/build.sh"
    local dir; dir=$(lib_dir)
    mkdir -p "$local_state"
    adb shell am force-stop $pkg
    su_sh "mkdir -p $state && chmod 777 $state"

    # Always patch the device's own libmain.so, and keep the pristine copy.
    adb shell "su 0 cat $dir/libmain.so" > "$local_state/libmain.device.so"
    if patchelf --print-needed "$local_state/libmain.device.so" | grep -qx $probe; then
        echo "libmain.so already loads the probe; refreshing the probe only"
    else
        cp "$local_state/libmain.device.so" "$local_state/libmain.orig.so"
        adb push -q "$local_state/libmain.orig.so" $state/libmain.orig.so
        cp "$local_state/libmain.orig.so" "$local_state/libmain.patched.so"
        patchelf --add-needed $probe "$local_state/libmain.patched.so"
        adb push -q "$local_state/libmain.patched.so" $state/libmain.patched.so
        # Overwrite in place: keeps the file's owner and SELinux label.
        su_sh "cat $state/libmain.patched.so > $dir/libmain.so"
    fi
    adb push -q "$here/build/$probe" $state/$probe
    su_sh "cp $state/$probe $dir/$probe && chown system:system $dir/$probe && chmod 755 $dir/$probe && chcon u:object_r:apk_data_file:s0 $dir/$probe"
    ports
    echo "installed. Start MTGA, then: adb logcat -s mtgacoach"
}

uninstall_probe() {
    local dir; dir=$(lib_dir)
    adb shell am force-stop $pkg
    if adb shell "su 0 ls $state/libmain.orig.so" >/dev/null 2>&1; then
        su_sh "cat $state/libmain.orig.so > $dir/libmain.so"
    fi
    su_sh "rm -f $dir/$probe"
    adb reverse --remove tcp:44222 2>/dev/null || true
    adb forward --remove tcp:$diag_port 2>/dev/null || true
    echo "uninstalled (libmain.so restored)"
}

status() {
    local dir; dir=$(lib_dir)
    echo "lib dir: $dir"
    su_sh "ls -laZ $dir/libmain.so $dir/$probe 2>&1" || true
    adb shell "su 0 cat $dir/libmain.so" > "${TMPDIR:-/tmp}/libmain.status.so"
    echo "libmain NEEDED: $(patchelf --print-needed "${TMPDIR:-/tmp}/libmain.status.so" | tr '\n' ' ')"
    echo "adb reverse: $(adb reverse --list | tr '\n' ' ')"
    echo "--- probe log (logcat, last 20)"
    adb logcat -d -s mtgacoach | tail -20
}

case ${1:-status} in
    install) install_probe ;;
    uninstall) uninstall_probe ;;
    status) status ;;
    ports) ports ;;
    mtu-fix) su_sh "sysctl -w net.ipv4.tcp_mtu_probing=1" ;;
    *) echo "usage: $0 install|uninstall|status|ports|mtu-fix" >&2; exit 2 ;;
esac

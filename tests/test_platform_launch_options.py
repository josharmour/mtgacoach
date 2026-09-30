"""Steam launch-option checks for the bridge (Proton override, native-Mac injection)."""

from pathlib import Path

import pytest

from arenamcp import platform_integration as pi
from arenamcp.platform_integration import MtgaInstall


def install_with_launch_options(tmp_path: Path, *options: str | None) -> MtgaInstall:
    """A Steam root whose userdata holds one localconfig.vdf per option (None = no entry)."""
    for index, option in enumerate(options):
        config = tmp_path / "userdata" / str(1000 + index) / "config"
        config.mkdir(parents=True)
        entry = f'\t\t\t\t\t"LaunchOptions"\t\t"{option}"\n' if option is not None else ""
        (config / "localconfig.vdf").write_text(
            '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
            f'\t\t\t\t"apps"\n\t\t\t\t{{\n\t\t\t\t\t"{pi.MTGA_STEAM_APPID}"\n\t\t\t\t\t{{\n'
            f'\t\t\t\t\t\t"LastPlayed"\t\t"1790000000"\n{entry}\t\t\t\t\t}}\n\t\t\t\t}}\n'
            "\t\t\t}\n\t\t}\n\t}\n}\n"
        )
    return MtgaInstall(install_dir=tmp_path / "MTGA", player_log=None, platform="x", steam_root=tmp_path)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (['WINEDLLOVERRIDES=\\"winhttp=n,b\\" %command%'], True),
        (["%command% -force-gfx", 'WINEDLLOVERRIDES=\\"winhttp=n,b\\" %command%'], True),
        (["%command%"], False),
        ([None], None),
        ([], None),
    ],
)
def test_proton_launch_options(tmp_path, options, expected):
    assert pi.proton_launch_options_ok(install_with_launch_options(tmp_path, *options)) is expected


def test_launch_options_undetermined_without_steam_root():
    install = MtgaInstall(install_dir=Path("/nowhere"), player_log=None, platform="x")
    assert pi.proton_launch_options_ok(install) is None
    assert pi.mac_bridge_launch_options_ok(install) is None


def test_mac_bridge_launch_option_round_trips_through_steam_config(tmp_path):
    escaped = pi.mac_bridge_launch_option().replace('"', '\\"')
    assert pi.mac_bridge_launch_options_ok(install_with_launch_options(tmp_path, escaped)) is True
    assert pi.mac_bridge_launch_option().endswith("%command%")
    assert str(pi.MAC_BRIDGE_LIBRARY) in pi.mac_bridge_launch_option()


def test_mac_bridge_launch_option_missing(tmp_path):
    assert pi.mac_bridge_launch_options_ok(install_with_launch_options(tmp_path, "%command%")) is False


@pytest.mark.parametrize(
    ("installed", "connected", "expected"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_mac_bridge_library_selects_the_gre_engine(monkeypatch, installed, connected, expected):
    from arenamcp.native_mac_autopilot import use_native_mac_autopilot

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: installed)
    monkeypatch.setattr("arenamcp.native_mac_autopilot.mac_gre_bridge_connected", lambda: connected)
    assert use_native_mac_autopilot() is expected


@pytest.mark.parametrize(
    ("platform", "bepinex_capable", "device", "expected", "excluded"),
    [
        ("darwin", False, "desktop", "DYLD_INSERT_LIBRARIES", "BepInEx"),
        ("darwin", True, "desktop", "BepInEx", "DYLD_INSERT_LIBRARIES"),
        ("linux", True, "desktop", "WINEDLLOVERRIDES", "DYLD_INSERT_LIBRARIES"),
        ("win32", True, "desktop", "BepInEx", "WINEDLLOVERRIDES"),
        ("darwin", False, "android", "adb reverse", "DYLD_INSERT_LIBRARIES"),
    ],
)
def test_bridge_recovery_hint_matches_game_device(
    monkeypatch, platform, bepinex_capable, device, expected, excluded
):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(pi, "bridge_capable", lambda: bepinex_capable)
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", device)
    hint = pi.bridge_offline_hint()
    assert expected in hint
    assert excluded not in hint

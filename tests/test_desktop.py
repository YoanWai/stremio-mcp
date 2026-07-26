import pytest

from stremio_mcp import desktop

DOWNLOADS_HTML = """
<a href="https://dl.strem.io/stremio-shell-macos/v5.1.24/Stremio_arm64.dmg">mac arm</a>
<a href="https://dl.strem.io/stremio-shell-macos/v5.1.24/Stremio_x64.dmg">mac intel</a>
<a href="https://dl.strem.io/stremio-shell-ng/v5.0.22/StremioSetup-v5.0.22_x64.exe">win</a>
<a href="https://dl.strem.io/stremio-shell-ng/v5.0.22/StremioSetup-v5.0.22_arm64.exe">win arm</a>
<a href="https://dl.strem.io/shell-linux/v4.4.168/stremio_4.4.168-1_amd64.deb">linux</a>
<a href="https://dl.strem.io/android/v2.3.2-android/com.stremio.one-2.3.2.apk">android</a>
"""


def test_download_link_regex_finds_desktop_installers():
    links = [
        link
        for link in dict.fromkeys(desktop._DOWNLOAD_LINK_RE.findall(DOWNLOADS_HTML))
        if "/android/" not in link
    ]
    assert len(links) == 5
    assert all("/android/" not in link for link in links)


@pytest.mark.parametrize(
    "system,fragment,suffix",
    [
        ("Darwin", "stremio-shell-macos", ".dmg"),
        ("Windows", "stremio-shell-ng", ".exe"),
        ("Linux", "shell-linux", ".deb"),
    ],
)
def test_every_supported_platform_matches_a_real_link(system, fragment, suffix):
    rule_fragment, rule_suffix, _ = desktop._INSTALLER_RULES[system]
    assert (rule_fragment, rule_suffix) == (fragment, suffix)
    links = desktop._DOWNLOAD_LINK_RE.findall(DOWNLOADS_HTML)
    matching = [link for link in links if fragment in link and link.endswith(suffix)]
    assert matching, f"no {system} installer matched"


def test_arch_token_falls_back_to_x64_for_another_machine(monkeypatch):
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    assert desktop._arch_token() == "arm64"
    assert desktop._arch_token("Windows") == "x64"


def test_arch_token_maps_intel(monkeypatch):
    monkeypatch.setattr(desktop.platform, "machine", lambda: "x86_64")
    assert desktop._arch_token() == "x64"


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
def test_data_dir_is_platform_specific(monkeypatch, system):
    monkeypatch.setattr(desktop.platform, "system", lambda: system)
    assert "stremio-server" in str(desktop.data_dir())


def test_app_path_is_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(desktop.platform, "system", lambda: "Linux")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.app_path() is None

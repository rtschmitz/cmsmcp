"""macOS/Windows accommodations: Linux behaviour unchanged, other platforms simulated."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import pytest

from cmsmcp import oscompat


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oscompat, "IS_WINDOWS", False)
    monkeypatch.setattr(oscompat, "IS_MACOS", False)
    monkeypatch.setattr(oscompat, "UNSUPPORTED_PLATFORM", None)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(oscompat, "IS_WINDOWS", True)
    monkeypatch.setattr(oscompat, "IS_MACOS", False)
    monkeypatch.setattr(oscompat, "UNSUPPORTED_PLATFORM", "Windows")
    for name in ("APPDATA", "LOCALAPPDATA", "PROGRAMFILES"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oscompat, "IS_WINDOWS", False)
    monkeypatch.setattr(oscompat, "IS_MACOS", True)
    monkeypatch.setattr(oscompat, "UNSUPPORTED_PLATFORM", "macOS")


@pytest.mark.usefixtures("linux")
def test_linux_paths_and_checks_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("X509_USER_PROXY", raising=False)
    assert oscompat.config_root() == Path.home() / ".config"
    assert oscompat.state_root() == Path.home() / ".local" / "state"
    assert oscompat.shared_with_others(0o100640)
    assert not oscompat.shared_with_others(0o100600)
    assert oscompat.browser_candidates() == []
    assert oscompat.default_proxy_path() == Path(f"/tmp/x509up_u{os.getuid()}")  # noqa: S108
    assert oscompat.kerberos_command("kinit") == "kinit"
    assert oscompat.kerberos_install_hint("krb5-user") == "krb5-user"
    assert oscompat.kerberos_extra_hint("needs libkrb5-dev") == "needs libkrb5-dev"
    assert oscompat.lxplus_unsupported() is None
    assert oscompat.extra_subprocess_env() == {}
    assert oscompat.safe_filename("a:b?.pdf") == "a:b?.pdf"


def test_xdg_variables_win_everywhere(windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(windows / "xdg"))
    assert oscompat.config_root() == windows / "xdg"


def test_windows_roots_permissions_and_lxplus(windows: Path) -> None:
    assert oscompat.config_root() == windows / "APPDATA"
    assert oscompat.state_root() == windows / "LOCALAPPDATA"
    # Windows reports 0o666 for every file: POSIX bits must not reject secrets.
    assert not oscompat.shared_with_others(0o100666)
    assert "WSL" in (oscompat.lxplus_unsupported() or "")
    assert "MIT Kerberos" in oscompat.kerberos_install_hint("krb5-user")


def test_windows_ignores_system32_klist(windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(oscompat.shutil, "which", lambda _: r"C:\Windows\System32\klist.exe")
    assert oscompat.kerberos_tool("klist") is None
    mit = windows / "PROGRAMFILES" / "MIT" / "Kerberos" / "bin"
    mit.mkdir(parents=True)
    (mit / "klist.exe").write_bytes(b"")
    assert oscompat.kerberos_tool("klist") == str(mit / "klist.exe")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("slides.pdf", "slides.pdf"),
        ("a:b.pdf", "a_b.pdf"),
        ('q?"<>|*.txt', "q______.txt"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("trailing. ", "trailing"),
        ("...", "download"),
    ],
)
@pytest.mark.usefixtures("windows")
def test_windows_download_names(name: str, expected: str) -> None:
    assert oscompat.safe_filename(name) == expected


def test_windows_browser_candidates(windows: Path) -> None:
    edge = windows / "PROGRAMFILES" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    assert oscompat.find_browser(("no-such-browser-on-path",)) == str(edge)


@pytest.mark.usefixtures("macos")
def test_macos_browser_candidates_are_app_bundles() -> None:
    assert "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" in (
        oscompat.browser_candidates()
    )


@pytest.mark.usefixtures("macos")
def test_off_linux_trust_includes_cern_grid_ca() -> None:
    subjects = {
        dict(item[0] for item in cert["subject"]).get("commonName")
        for cert in oscompat.trust_context().get_ca_certs()
    }
    assert {
        "CERN Root Certification Authority 2",
        "CERN Grid Certification Authority",
        "CERN Root Certification Authority 3",
        "CERN Grid Certification Authority 2",
    } <= subjects


@pytest.mark.usefixtures("linux")
def test_linux_trust_is_the_plain_system_context() -> None:
    plain = ssl.create_default_context()
    assert len(oscompat.trust_context().get_ca_certs()) == len(plain.get_ca_certs())


# --- AFS home (CERN lxplus) ---------------------------------------------------


@pytest.fixture
def afs_home(linux: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:  # noqa: ARG001
    afs = tmp_path / "afs"
    home = afs / "cern.ch" / "user" / "j" / "jdoe"
    (home / "private").mkdir(parents=True)
    monkeypatch.setattr(oscompat, "_AFS_ROOT", afs)
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "X509_USER_PROXY",
        "PLAYWRIGHT_BROWSERS_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def test_afs_home_defaults(afs_home: Path) -> None:
    data = afs_home / "private" / "cmsmcp"
    assert oscompat.afs_data_dir() == data
    assert oscompat.default_proxy_path() == afs_home / f"x509up_u{os.getuid()}"
    assert oscompat.lxplus_unsupported() == oscompat.LXPLUS_ON_AFS
    oscompat.prepare_environment()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(data / "pw")


def test_afs_home_respects_explicit_settings(
    afs_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("X509_USER_PROXY", "/elsewhere/proxy")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/elsewhere/pw")
    assert oscompat.default_proxy_path() == Path("/elsewhere/proxy")
    oscompat.prepare_environment()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/elsewhere/pw"
    # lxplus tools work again once the state (socket) directory is off AFS
    monkeypatch.setenv("CMSMCP_STATE_DIR", str(tmp_path / "local-state"))
    assert oscompat.lxplus_unsupported() is None
    monkeypatch.setenv("CMSMCP_STATE_DIR", str(afs_home / "state"))
    assert oscompat.lxplus_unsupported() == oscompat.LXPLUS_ON_AFS


def test_afs_home_without_private_or_off_afs_keeps_linux_defaults(
    afs_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (afs_home / "private").rmdir()
    assert oscompat.afs_data_dir() is None
    elsewhere = tmp_path / "home" / "jdoe"
    (elsewhere / "private").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(elsewhere))
    assert oscompat.afs_data_dir() is None
    assert oscompat.default_proxy_path() == Path(f"/tmp/x509up_u{os.getuid()}")  # noqa: S108
    assert oscompat.lxplus_unsupported() is None
    oscompat.prepare_environment()
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


async def test_afs_status_reports_lxplus_tools_off(afs_home: Path, tmp_path: Path) -> None:  # noqa: ARG001
    from cmsmcp.config.settings import Settings  # noqa: PLC0415
    from cmsmcp.credentials import LXPLUS_OFF_ON_AFS, OK, gather_status  # noqa: PLC0415

    rows = dict(await gather_status(Settings(state_dir=tmp_path / "state"), live=False))
    assert (rows["lxplus"].state, rows["lxplus"].detail) == (OK, LXPLUS_OFF_ON_AFS)

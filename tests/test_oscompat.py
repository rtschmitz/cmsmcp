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

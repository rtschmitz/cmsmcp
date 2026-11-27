"""Typed settings behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cmsmcp import oscompat
from cmsmcp.config.settings import Settings


def test_settings_derives_state_paths(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path)
    assert settings.session_path == tmp_path / "cern-sso-storage-state.json"
    assert settings.download_dir == tmp_path / "downloads"
    assert settings.require_sso is True
    assert settings.session_max_age_seconds == 24 * 60 * 60
    assert "*.cern.ch" in settings.allowed_hosts


def test_comma_separated_environment_style_lists_are_typed() -> None:
    settings = Settings(
        identity_cookie_names="KEYCLOAK_IDENTITY,OTHER_ID",
        allowed_hosts="*.cern.ch,inspirehep.net",
    )
    assert settings.identity_cookie_names == ("KEYCLOAK_IDENTITY", "OTHER_ID")
    assert settings.allowed_hosts == ("*.cern.ch", "inspirehep.net")


def test_state_directories_use_owner_permissions(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    settings.state_dir.mkdir(parents=True)
    settings.state_dir.chmod(0o755)
    settings.ensure_directories()
    assert settings.state_dir.is_dir()
    assert settings.download_dir.is_dir()
    assert (settings.state_dir.stat().st_mode & 0o777) == 0o700
    assert (settings.download_dir.stat().st_mode & 0o777) == 0o700


def test_documented_env_example_parses_as_settings() -> None:
    env_example = Path(__file__).parents[1] / ".env.example"
    settings = Settings(_env_file=env_example)
    assert settings.identity_cookie_names == ("KEYCLOAK_IDENTITY",)
    assert "*.cern.ch" in settings.allowed_hosts
    assert settings.state_dir.is_absolute()


def test_cwd_dotenv_cannot_disable_sso(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("CMSMCP_REQUIRE_SSO=false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CMSMCP_REQUIRE_SSO", raising=False)
    assert Settings().require_sso is True


def test_afs_home_defaults_live_in_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    afs = tmp_path / "afs"
    home = afs / "cern.ch" / "user" / "j" / "jdoe"
    (home / "private").mkdir(parents=True)
    monkeypatch.setattr(oscompat, "_AFS_ROOT", afs)
    monkeypatch.setenv("HOME", str(home))
    for name in ("CMSMCP_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "X509_USER_PROXY"):
        monkeypatch.delenv(name, raising=False)
    data = home / "private" / "cmsmcp"
    proxy = home / f"x509up_u{oscompat.os.getuid()}"

    settings = Settings()
    assert settings.config_dir == data / "config"
    assert settings.state_dir == data / "state"
    assert settings.grid_cert_file == proxy
    assert settings.grid_key_file == proxy

    monkeypatch.setenv("CMSMCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    explicit = Settings(grid_cert_file=tmp_path / "cert.pem")
    assert explicit.config_dir == tmp_path / "cfg"
    assert explicit.state_dir == tmp_path / "xdg" / "cmsmcp"
    assert explicit.grid_cert_file == tmp_path / "cert.pem"

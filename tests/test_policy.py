"""Policy engine: read-only effects, personal opt-in, audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cmsmcp.config.settings import Settings
from cmsmcp.policy import Effect, PersonalSourceDisabledError, Policy


def test_only_read_effects_exist() -> None:
    assert {str(effect) for effect in Effect} == {"read", "read-personal"}


def test_personal_sources_opt_in(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    with pytest.raises(PersonalSourceDisabledError):
        policy.require_personal("mattermost_dm", "a direct message")
    Policy(Settings(state_dir=tmp_path, personal_sources=("mattermost_dm",))).require_personal(
        "mattermost_dm", "a direct message"
    )
    assert Policy(Settings(state_dir=tmp_path, personal_sources="all")).personal_allowed("x")


def test_audit_log_is_private_and_clips_values(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    policy.audit("t", Effect.READ, {"q": "x" * 500}, status="ok", elapsed=0.01)
    path = tmp_path / "audit.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text().splitlines()[0])
    assert record["tool"] == "t" and record["status"] == "ok"
    assert len(record["args"]["q"]) < 260


async def test_mattermost_dm_reads_need_personal_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cmsmcp.adapters import mattermost  # noqa: PLC0415
    from cmsmcp.app import build_server  # noqa: PLC0415

    async def channel_type(*_a: Any, **_k: Any) -> str:
        return "D"

    async def get_posts(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"source": "mattermost", "posts": [{"message": "private"}]}

    monkeypatch.setattr(mattermost, "channel_type", channel_type)
    monkeypatch.setattr(mattermost, "get_posts", get_posts)
    args = {"channel_id": "a" * 26}

    closed = build_server(Settings(state_dir=tmp_path, require_sso=False))
    refused = json.loads((await closed.call_tool("mattermost_get_posts", args)).content[0].text)
    assert refused["status"] == "error" and "personal" in refused["error"]["message"]
    search = await closed.call_tool(
        "mattermost_search_posts",
        {"team_id": "t" * 26, "terms": "x", "include_direct_messages": True},
    )
    assert json.loads(search.content[0].text)["status"] == "error"

    opened = build_server(
        Settings(state_dir=tmp_path, require_sso=False, personal_sources=("mattermost_dm",))
    )
    allowed = json.loads((await opened.call_tool("mattermost_get_posts", args)).content[0].text)
    assert allowed["status"] == "ok"


def test_audit_redacts_url_secrets(tmp_path: Path) -> None:
    policy = Policy(Settings(state_dir=tmp_path))
    policy.audit(
        "cern_download_file",
        Effect.READ,
        {"url": "https://x.cern.ch/f?access_token=SECRETVALUE"},
        status="ok",
        elapsed=0.0,
    )
    assert "SECRETVALUE" not in (tmp_path / "audit.jsonl").read_text()

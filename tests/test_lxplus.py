"""lxplus adapter and tools with SSH stubbed out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cmsmcp.adapters import lxplus
from cmsmcp.app import build_server
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, PolicyViolationError
from cmsmcp.net.proc import CommandResult


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run(argv: list[str], **_kw: Any) -> CommandResult:
        calls.append(list(argv))
        return CommandResult(exit_code=0, stdout=b"remote output\n", stderr=b"")

    async def ticket() -> None:
        return None

    monkeypatch.setattr(lxplus, "run_command", fake_run)
    monkeypatch.setattr(lxplus, "_ensure_ticket", ticket)
    return calls


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(state_dir=tmp_path, lxplus_user="jdoe", ssh_executable="/usr/bin/ssh", **kw)


async def test_eos_ls_quotes_the_remote_command(tmp_path: Path, ssh: list[list[str]]) -> None:
    out = await lxplus.eos_ls(_settings(tmp_path), path="/eos/user/j/jdoe/my dir")
    argv = ssh[0]
    assert argv[0] == "/usr/bin/ssh"
    assert "GSSAPIAuthentication=yes" in argv and "BatchMode=yes" in argv
    assert argv[-2] == "jdoe@lxplus.cern.ch"
    assert argv[-1] == "eos ls -l '/eos/user/j/jdoe/my dir'"
    assert out["output"] == "remote output\n"


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "eos/user/x", "/eos/user/../../etc", "/eos/x;rm -rf ~", "/eos/$(id)"]
)
def test_paths_are_validated(bad: str) -> None:
    with pytest.raises(InvalidInputError):
        lxplus.check_path(bad)


@pytest.mark.parametrize(
    "path",
    [
        "/afs/cern.ch/user/j/jdoe/.ssh/id_ed25519",
        "/afs/cern.ch/user/j/jdoe/private/jdoe.keytab",
        "/afs/cern.ch/user/j/jdoe/.globus/userkey.pem",
        "/eos/user/j/jdoe/private/notes.txt",
    ],
)
async def test_reads_never_touch_credentials(
    tmp_path: Path, ssh: list[list[str]], path: str
) -> None:
    with pytest.raises(PolicyViolationError):
        await lxplus.read_file(_settings(tmp_path), path=path)
    with pytest.raises(PolicyViolationError):
        await lxplus.download_file(_settings(tmp_path), path=path)
    assert ssh == []


async def test_crab_status_only_from_own_areas(tmp_path: Path, ssh: list[list[str]]) -> None:
    settings = _settings(tmp_path)
    foreign = "/afs/cern.ch/user/a/attacker/public/CMSSW_13_0_0/src"
    with pytest.raises(PolicyViolationError):
        await lxplus.crab_status(settings, cmssw_src=foreign, task_dir="crab_x")
    with pytest.raises(PolicyViolationError):
        await lxplus.crab_status(
            settings,
            cmssw_src="/afs/cern.ch/user/j/jdoe/CMSSW_13_0_0/src",
            task_dir="/eos/user/a/attacker/crab_x",
        )
    assert ssh == []
    await lxplus.crab_status(
        settings, cmssw_src="/afs/cern.ch/work/j/jdoe/CMSSW_13_0_0/src", task_dir="crab_ok"
    )
    assert ssh[-1][-1].endswith("crab status -d crab_ok")


async def test_unconfigured_user_points_to_setup(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError) as caught:
        await lxplus.eos_ls(Settings(state_dir=tmp_path), path="/eos/user/j")
    assert "cmsmcp setup lxplus" in (caught.value.instructions or "")


async def test_download_writes_private_file(tmp_path: Path, ssh: list[list[str]]) -> None:
    del ssh
    out = await lxplus.download_file(_settings(tmp_path), path="/eos/user/j/jdoe/plot.png")
    path = Path(out["path"])
    assert path.read_bytes() == b"remote output\n"
    assert path.stat().st_mode & 0o777 == 0o600


async def test_only_read_tools_are_registered(tmp_path: Path) -> None:
    names = {t.name for t in await build_server(_settings(tmp_path)).list_tools()}
    lxplus_tools = {name for name in names if name.startswith("lxplus_")}
    assert lxplus_tools == {
        "lxplus_eos_ls",
        "lxplus_read_file",
        "lxplus_download_file",
        "lxplus_quota",
        "lxplus_condor_queue",
        "lxplus_condor_history",
        "lxplus_crab_status",
    }

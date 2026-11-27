"""lxplus access over the user's Kerberos ticket (SSH + GSSAPI).

Endpoint type: OpenSSH to ``lxplus.cern.ch`` with ``GSSAPIAuthentication`` and
credential delegation, non-interactive (``BatchMode``), no passwords or keys,
and a 10-minute shared control connection so repeated tool calls reuse one
login. Requires ``kinit`` and ``CMSMCP_LXPLUS_USER`` (``cmsmcp setup lxplus``).

Read-only: every tool runs one fixed command shape (``eos ls``, ``head -c``,
``cat``, ``condor_q``, ``condor_history``, ``crab status``, quota queries)
whose arguments are validated and shell-quoted. There is no generic command,
submit, remove or resubmit path; paths that point at credential material
(Kerberos caches, keytabs, SSH keys, grid keys/proxies, cmsmcp secrets) or
``~/private`` are refused.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any

from cmsmcp import oscompat
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import (
    AuthRequiredError,
    InvalidInputError,
    PolicyViolationError,
    UpstreamError,
    setup_hint,
)
from cmsmcp.credentials import ssh_options
from cmsmcp.net.proc import CommandResult, run_command

SOURCE = "lxplus"
_PATH_RE = re.compile(r"^/(eos|afs|cvmfs)/[A-Za-z0-9._@+=,:/~ -]*$")
_JOB_RE = re.compile(r"^\d+(\.\d+)?$")
_SSH_FAILURE = 255
_TICKET_CACHE_SECONDS = 60.0
_DEFAULT_OUTPUT_BYTES = 2 * 1024 * 1024
_CREDENTIAL_PATTERNS = re.compile(
    r"krb5cc|keytab|\.ssh\b|id_(rsa|ed25519|ecdsa)|x509up|userkey|\.globus|"
    r"\.config/cmsmcp|cern-sso-storage-state|\bkdestroy\b|\bkinit\b",
    re.IGNORECASE,
)

_ticket_checked_at = 0.0


def check_path(path: str) -> str:
    """Accept only absolute /eos, /afs or /cvmfs paths without traversal or credentials.

    Applies to every lxplus tool, reads included: a read that returned a key,
    keytab, ticket cache or proxy would hand the credential to the model with
    no confirmation (the path is shell-quoted and never globbed, so this path
    denylist holds, unlike the best-effort one for free-form commands).
    """
    cleaned = path.strip()
    if not _PATH_RE.fullmatch(cleaned) or "/../" in f"{cleaned}/" or cleaned.endswith("/.."):
        raise InvalidInputError(
            f"unsupported path {path!r}",
            instructions="Use an absolute /eos/..., /afs/... or /cvmfs/... path without '..'.",
        )
    if _CREDENTIAL_PATTERNS.search(cleaned) or "/private/" in f"{cleaned}/":
        raise PolicyViolationError(
            "the path points at credential material or a private directory",
            instructions="cmsmcp never reads keys, tickets, keytabs, proxies or ~/private.",
        )
    return cleaned


def own_areas(settings: Settings) -> tuple[str, ...]:
    """The user's own AFS/EOS areas (code is only ever run from these)."""
    user = settings.lxplus_user or ""
    initial = user[:1]
    return (
        f"/afs/cern.ch/user/{initial}/{user}/",
        f"/afs/cern.ch/work/{initial}/{user}/",
        f"/eos/user/{initial}/{user}/",
        f"/eos/home-{initial}/{user}/",
        f"/eos/cms/store/user/{user}/",
    )


def check_own_path(settings: Settings, path: str) -> str:
    """A path inside the user's own areas: anything that *executes* project
    files (CRAB/SCRAM environments, submit files) must not run code from
    another user's readable directory."""
    cleaned = check_path(path)
    if not settings.lxplus_user or not f"{cleaned}/".startswith(own_areas(settings)):
        raise PolicyViolationError(
            f"{path!r} is not in your own AFS/EOS area",
            instructions="Only directories under your own /afs/cern.ch/{user,work}/ or "
            "/eos/user/ (or /eos/cms/store/user/) areas can be used here.",
        )
    return cleaned


def check_job_id(job_id: str) -> str:
    cleaned = job_id.strip()
    if not _JOB_RE.fullmatch(cleaned):
        raise InvalidInputError(
            f"bad HTCondor job id {job_id!r}", instructions="Use a cluster id like 1234 or 1234.0."
        )
    return cleaned


def _target(settings: Settings) -> str:
    if unsupported := oscompat.lxplus_unsupported():
        raise UpstreamError(unsupported)
    if not settings.lxplus_user:
        raise InvalidInputError(
            "lxplus access is not configured",
            instructions=setup_hint("lxplus", "lxplus access (Kerberos SSH)"),
        )
    if settings.ssh_executable is None:
        raise UpstreamError("ssh is not installed on this machine")
    return f"{settings.lxplus_user}@{settings.lxplus_host}"


async def _ensure_ticket() -> None:
    global _ticket_checked_at  # noqa: PLW0603 - process-wide short cache
    if time.monotonic() - _ticket_checked_at < _TICKET_CACHE_SECONDS:
        return
    klist = oscompat.kerberos_tool("klist")
    if klist is None or (await run_command([klist, "-s"], timeout_seconds=10)).exit_code != 0:
        error = AuthRequiredError("no valid Kerberos ticket for lxplus")
        error.instructions = (
            "Ask the user to run `kinit <user>@CERN.CH` in a terminal "
            "(or `cmsmcp setup kerberos`), then retry."
        )
        raise error
    _ticket_checked_at = time.monotonic()


async def run_remote(
    settings: Settings,
    remote_argv: list[str] | None = None,
    *,
    shell_command: str | None = None,
    timeout_seconds: float | None = None,
    max_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
) -> CommandResult:
    """Run one command on lxplus; ``remote_argv`` is quoted, ``shell_command`` verbatim."""
    target = _target(settings)
    await _ensure_ticket()
    if (remote_argv is None) == (shell_command is None):
        raise ValueError("pass exactly one of remote_argv / shell_command")
    remote = shell_command if shell_command is not None else shlex.join(remote_argv or [])
    result = await run_command(
        [str(settings.ssh_executable), *ssh_options(settings), target, remote],
        timeout_seconds=timeout_seconds or settings.ssh_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if result.exit_code == _SSH_FAILURE:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise UpstreamError(
            f"no usable lxplus connection ({detail[-1] if detail else 'ssh failed'})",
            instructions=(
                "lxplus needs a second factor after Kerberos, so cmsmcp reuses a connection "
                "the user opens once: ask them to run `cmsmcp connect` in a terminal "
                "(Kerberos + 2FA code, then reused for hours), and retry."
            ),
        )
    return result


def _result(command: str, result: CommandResult, *, limit: int | None = None) -> dict[str, Any]:
    out = result.stdout.decode(errors="replace")
    err = result.stderr.decode(errors="replace").strip()
    if limit is not None and len(out) > limit:
        out = f"{out[:limit]}\n…[+{len(out) - limit} chars]"
    payload: dict[str, Any] = {"command": command, "exit_code": result.exit_code, "output": out}
    if err:
        payload["stderr"] = err[-4_000:]
    return payload


async def eos_ls(settings: Settings, *, path: str) -> dict[str, Any]:
    path = check_path(path)
    argv = ["eos", "ls", "-l", path] if path.startswith("/eos/") else ["ls", "-la", path]
    return _result(shlex.join(argv), await run_remote(settings, argv))


async def read_file(settings: Settings, *, path: str, max_bytes: int = 200_000) -> dict[str, Any]:
    path = check_path(path)
    argv = ["head", "-c", str(max_bytes + 1), "--", path]
    result = await run_remote(settings, argv, max_output_bytes=max_bytes + 1)
    if b"\x00" in result.stdout[:8192]:
        raise InvalidInputError(
            f"{path} is a binary file",
            instructions="Use lxplus_download_file to fetch it into local storage.",
        )
    payload = _result(shlex.join(argv), result, limit=max_bytes)
    payload["truncated"] = len(result.stdout) > max_bytes
    return payload


async def quota(settings: Settings) -> dict[str, Any]:
    script = "eos quota 2>&1 | head -40; echo; fs listquota ~ 2>&1"
    return _result("eos quota; fs listquota ~", await run_remote(settings, shell_command=script))


async def condor_queue(settings: Settings, *, cluster: str | None = None) -> dict[str, Any]:
    argv = ["condor_q", "-nobatch", "-wide"]
    if cluster:
        argv.append(check_job_id(cluster))
    return _result(shlex.join(argv), await run_remote(settings, argv))


async def condor_history(settings: Settings, *, limit: int = 20) -> dict[str, Any]:
    argv = [
        "condor_history",
        str(settings.lxplus_user),
        "-limit",
        str(limit),
        "-af",
        "ClusterId",
        "ProcId",
        "JobStatus",
        "ExitCode",
        "CompletionDate",
        "Cmd",
    ]
    result = _result(shlex.join(argv), await run_remote(settings, argv))
    result["columns"] = "ClusterId ProcId JobStatus(4=done,3=removed,5=held) ExitCode Completed Cmd"
    return result


async def download_file(settings: Settings, *, path: str) -> dict[str, Any]:
    path = check_path(path)
    result = await run_remote(
        settings, ["cat", "--", path], max_output_bytes=settings.max_download_bytes
    )
    if result.exit_code != 0:
        raise UpstreamError(
            f"could not read {path}: {result.stderr.decode(errors='replace').strip()[-300:]}"
        )
    settings.ensure_directories()
    target = settings.download_dir / oscompat.safe_filename(Path(path).name)
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = settings.download_dir / f"{stem}-{n}{suffix}"
        n += 1
    target.write_bytes(result.stdout)
    target.chmod(0o600)
    return {"remote_path": path, "path": str(target), "size_bytes": len(result.stdout)}


def crab_script(settings: Settings, cmssw_src: str, task_dir: str) -> str:
    """``crab status`` inside the user's *own* CMSSW area (cmsenv + CRAB from CVMFS).

    ``scramv1 runtime`` output is eval'd and CRAB unpickles the task's request
    cache, so both directories must be the user's own: a path in another
    user's readable area would run their code as this user.
    """
    src = check_own_path(settings, cmssw_src)
    task = check_own_path(settings, task_dir) if task_dir.startswith("/") else task_dir
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", task) or ".." in task:
        raise InvalidInputError(f"bad CRAB task directory {task_dir!r}")
    return (
        "source /cvmfs/cms.cern.ch/cmsset_default.sh"
        f" && cd {shlex.quote(src)}"
        " && eval $(scramv1 runtime -sh)"
        " && source /cvmfs/cms.cern.ch/common/crab-setup.sh"
        f" && crab status -d {shlex.quote(task)}"
    )


async def crab_status(settings: Settings, *, cmssw_src: str, task_dir: str) -> dict[str, Any]:
    script = crab_script(settings, cmssw_src, task_dir)
    return _result(f"crab status -d {task_dir}", await run_remote(settings, shell_command=script))

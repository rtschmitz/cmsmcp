"""Server-side policy: effect classes, personal-data opt-ins, audit.

cmsmcp is a read-only data-retrieval server. Every tool declares an
:class:`Effect`, and only read effects exist:

- ``read`` tools run freely;
- ``read-personal`` content (Mattermost DMs, ...) is refused unless the user
  opted the source in (``CMSMCP_PERSONAL_SOURCES``).

The registry refuses to register a tool with any other effect. Every tool call
is appended to a local audit log (``audit.jsonl``, owner-only) with clipped
argument values and no secrets.
"""

from __future__ import annotations

import json
import logging
import os
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError
from cmsmcp.contracts.results import ErrorCode
from cmsmcp.net.safety import redact_url

logger = logging.getLogger(__name__)

_AUDIT_VALUE_CLIP = 200


class Effect(StrEnum):
    """What a tool reads (cmsmcp tools never change remote state)."""

    READ = "read"
    READ_PERSONAL = "read-personal"


# Explicit, not frozenset(Effect): a new effect member must not become
# registrable by accident (the registry refuses anything outside this set).
READ_EFFECTS = frozenset({Effect.READ, Effect.READ_PERSONAL})


class PersonalSourceDisabledError(CMSMCPError):
    """Personal content requested without the user's opt-in."""

    code = ErrorCode.POLICY_VIOLATION


def _clip_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        value = redact_url(value)
    if isinstance(value, str) and len(value) > _AUDIT_VALUE_CLIP:
        return f"{value[:_AUDIT_VALUE_CLIP]}…[+{len(value) - _AUDIT_VALUE_CLIP}]"
    if isinstance(value, list | tuple):
        return [_clip_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return {k: _clip_value(v) for k, v in list(value.items())[:20]}
    return value


def _write_private(path: Path, text: str, *, append: bool) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a" if append else "w") as handle:
        handle.write(text)
    path.chmod(0o600)


class Policy:
    """Per-process policy state (one stdio server == one client session)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit_path = settings.state_dir / "audit.jsonl"

    # --- personal data ---------------------------------------------------
    def personal_allowed(self, source: str) -> bool:
        enabled = set(self.settings.personal_sources)
        return "all" in enabled or source in enabled

    def require_personal(self, source: str, what: str) -> None:
        if not self.personal_allowed(source):
            raise PersonalSourceDisabledError(
                f"{what} is personal content and is not enabled",
                instructions=(
                    "Only if the user asked for this: they can opt in with `cmsmcp setup "
                    f"policy` (or add '{source}' to CMSMCP_PERSONAL_SOURCES), then restart "
                    "the MCP server."
                ),
            )

    # --- audit -----------------------------------------------------------
    def audit(
        self,
        tool: str,
        effect: Effect,
        arguments: dict[str, Any],
        *,
        status: str,
        elapsed: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.audit_enabled:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool,
            "effect": str(effect),
            "args": {k: _clip_value(v) for k, v in arguments.items()},
            "status": status,
            "ms": round(elapsed * 1000),
        }
        if extra:
            record.update(extra)
        try:
            _write_private(self.audit_path, json.dumps(record, default=str) + "\n", append=True)
        except OSError:
            logger.debug("audit write failed", exc_info=True)

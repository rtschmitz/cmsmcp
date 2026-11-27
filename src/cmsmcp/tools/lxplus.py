"""MCP registration for lxplus (Kerberos SSH): read-only tools."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters import lxplus
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope
from cmsmcp.tools.common import enveloped

_SETUP = "Needs a Kerberos ticket and `cmsmcp setup lxplus`."


def register_lxplus_tools(server: MCPServer, settings: Settings) -> None:
    """Register the read-only lxplus tools."""

    @server.tool(
        name="lxplus_eos_ls",
        description=(
            "List a directory on EOS (/eos/...) or AFS (/afs/...) as the user, via lxplus. "
            + _SETUP
        ),
    )
    async def lxplus_eos_ls(path: str) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.eos_ls(settings, path=path))

    @server.tool(
        name="lxplus_read_file",
        description=(
            "Read the start of a text file on EOS/AFS/CVMFS (configs, logs, small CSV/JSON). "
            "Binary files (ROOT, PNG, PDF) must use lxplus_download_file. " + _SETUP
        ),
    )
    async def lxplus_read_file(
        path: str,
        max_bytes: Annotated[int, Field(ge=100, le=2_000_000)] = 200_000,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.read_file(settings, path=path, max_bytes=max_bytes))

    @server.tool(
        name="lxplus_download_file",
        description=(
            "Copy one EOS/AFS file (ROOT, plot, PDF, ...) into local cmsmcp storage; returns "
            "the local path, never the content. " + _SETUP
        ),
    )
    async def lxplus_download_file(path: str) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.download_file(settings, path=path))

    @server.tool(
        name="lxplus_quota",
        description="The user's EOS and AFS quota and usage. " + _SETUP,
    )
    async def lxplus_quota() -> ToolEnvelope:
        return await enveloped(lambda: lxplus.quota(settings))

    @server.tool(
        name="lxplus_condor_queue",
        description=(
            "The user's HTCondor jobs in the queue (idle/running/held), optionally one "
            "cluster. " + _SETUP
        ),
    )
    async def lxplus_condor_queue(
        cluster: Annotated[str | None, Field(description="Cluster id, e.g. 1234.")] = None,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.condor_queue(settings, cluster=cluster))

    @server.tool(
        name="lxplus_condor_history",
        description="The user's recently finished HTCondor jobs with exit codes. " + _SETUP,
    )
    async def lxplus_condor_history(
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> ToolEnvelope:
        return await enveloped(lambda: lxplus.condor_history(settings, limit=limit))

    @server.tool(
        name="lxplus_crab_status",
        description=(
            "`crab status` of one CRAB task, run in the user's own CMSSW area on lxplus "
            "(cmssw_src = .../CMSSW_X/src; task_dir = crab project dir, relative to it or "
            "absolute). Needs a valid grid proxy on lxplus. " + _SETUP
        ),
    )
    async def lxplus_crab_status(cmssw_src: str, task_dir: str) -> ToolEnvelope:
        return await enveloped(
            lambda: lxplus.crab_status(settings, cmssw_src=cmssw_src, task_dir=task_dir)
        )

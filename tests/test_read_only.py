"""cmsmcp is read-only: no tool, HTTP path or page render may change remote state."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cmsmcp.app import build_server
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import PolicyViolationError
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.net.safety import browser_request_allowed, check_read_only_post, check_read_only_url
from cmsmcp.policy import Effect
from cmsmcp.registry import CmsMcpServer


async def test_every_registered_tool_is_read_only(tmp_path: Path) -> None:
    server = build_server(Settings(state_dir=tmp_path, require_sso=False))
    tools = await server.list_tools()
    assert len(tools) > 150
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
    assert {info.effect for info in server.tool_infos.values()} <= set(Effect)


def test_registry_refuses_a_non_read_tool(tmp_path: Path) -> None:
    from cmsmcp.policy import Policy  # noqa: PLC0415

    settings = Settings(state_dir=tmp_path, require_sso=False)
    server = CmsMcpServer("t", settings=settings, policy=Policy(settings))
    with pytest.raises(ValueError, match="read-only tools only"):

        @server.tool(name="x_post", effect="act-external")  # type: ignore[arg-type]
        async def x_post() -> str:
            return "never"


def test_no_write_path_on_the_http_client() -> None:
    assert not hasattr(SafeHttpClient, "request_action")


@pytest.mark.parametrize(
    "url",
    [
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/approve/B2G-1",
        "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/reset/B2G-1",
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/delete/B2G-1",
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/chained_requests/flow/X",
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/%61pprove/B2G-1",
        "https://twiki.cern.ch/twiki/bin/save/CMS/Topic?text=x",
        "https://twiki.cern.ch/twiki/bin/rename/CMS/Topic",
        "https://twiki.cern.ch/twiki/bin/upload.pl/CMS/Topic",
        "https://cms-docdb.cern.ch/cgi-bin/DocDB/ProcessDocumentAdd?x=1",
        "https://cms-docdb.cern.ch/cgi-bin/DocDB/XMLUpload",
        "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/logout",
        "https://indico.cern.ch/logout/",
        "https://example.cern.ch/items/1?_method=DELETE",
    ],
)
def test_state_changing_urls_are_always_refused(url: str) -> None:
    with pytest.raises(PolicyViolationError):
        check_read_only_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get/B2G-1",
        "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_status_and_approval/X",
        "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/output/B2G-1",
        "https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/produces/A/B/C",
        "https://twiki.cern.ch/twiki/bin/view/CMS/Topic?cover=print",
        "https://twiki.cern.ch/twiki/bin/viewauth/CMS/Topic",
        "https://cms-docdb.cern.ch/cgi-bin/DocDB/ShowDocument?docid=1",
        "https://cms-docdb.cern.ch/cgi-bin/DocDB/RetrieveFile?docid=1",
        "https://indico.cern.ch/event/1/attachments/2/3/update.pdf",
        "https://gitlab.cern.ch/api/v4/projects/1/repository/files/src%2Fdelete.py/raw",
    ],
)
def test_reads_pass_even_strictly(url: str) -> None:
    assert check_read_only_url(url, strict=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.cern.ch/group/project/-/issues/1/delete",
        "https://example.cern.ch/api/subscribe",
        "https://example.cern.ch/page?action=delete",
        "https://example.cern.ch/a/b/approve",
    ],
)
def test_strict_mode_refuses_action_verbs(url: str) -> None:
    check_read_only_url(url)  # not a known service rule
    with pytest.raises(PolicyViolationError):
        check_read_only_url(url, strict=True)


def test_only_registered_post_endpoints() -> None:
    for url in (
        "https://cern.service-now.com/api/now/sp/search",
        "https://mattermost.web.cern.ch/api/v4/teams/abc123/posts/search",
        "https://cmsrunregistry.web.cern.ch/api/runs_filtered_ordered",
        "https://cmsrunregistry.web.cern.ch/api/lumisections/rr_lumisection_ranges",
        "https://cmsonline.cern.ch/Elog/json.do",
    ):
        assert check_read_only_post(url) == url
    for url in (
        "https://mattermost.web.cern.ch/api/v4/posts",
        "https://cern.service-now.com/api/now/table/incident",
        "https://cmsrunregistry.web.cern.ch/api/runs/edit",
        "https://cmsrunregistry.web.cern.ch/api/runs_filtered_ordered/../runs/edit",
    ):
        with pytest.raises(PolicyViolationError):
            check_read_only_post(url)


def _client(settings: Settings, handler) -> tuple[SafeHttpClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = SafeHttpClient(
        settings,
        AuthService(settings),
        client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    return client, seen


async def test_post_outside_the_registry_is_never_sent(settings: Settings) -> None:
    client, seen = _client(settings, lambda _r: httpx.Response(200))
    with pytest.raises(PolicyViolationError):
        await client.request_json_post(
            "https://mattermost.web.cern.ch/api/v4/posts",
            json_body={"channel_id": "x", "message": "hi"},
            allowlist=("mattermost.web.cern.ch",),
            authenticated=False,
        )
    assert seen == []


async def test_redirected_post_is_followed_as_a_bodyless_get(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/now/sp/search":
            return httpx.Response(307, headers={"location": "/elsewhere"})
        return httpx.Response(200, json={})

    client, seen = _client(settings, handler)
    await client.request_json_post(
        "https://cern.service-now.com/api/now/sp/search",
        json_body={"query": "x"},
        allowlist=("cern.service-now.com",),
        authenticated=False,
    )
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/api/now/sp/search"),
        ("GET", "/elsewhere"),
    ]
    assert seen[1].content == b""


async def test_get_and_redirects_refuse_state_changing_urls(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        target = "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/approve/X"
        return httpx.Response(302, headers={"location": target})

    client, seen = _client(settings, handler)
    with pytest.raises(PolicyViolationError):
        await client.request(
            "GET", "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/approve/X"
        )
    assert seen == []
    with pytest.raises(PolicyViolationError):
        await client.request("GET", "https://example.cern.ch/page", authenticated=False)
    assert [r.url.path for r in seen] == ["/page"]  # the redirect hop was never followed


async def test_downloads_are_strict(settings: Settings) -> None:
    client, seen = _client(settings, lambda _r: httpx.Response(200, content=b"x"))
    with pytest.raises(PolicyViolationError):
        await client.download("https://example.cern.ch/files/1/delete", authenticated=False)
    assert seen == []


def test_browser_allows_reads_and_sign_in_only() -> None:
    hosts = ("auth.cern.ch",)

    def allowed(method: str, url: str, body: str | None = None, *, nav: bool = False) -> bool:
        return browser_request_allowed(
            method, url, post_data=body, navigation=nav, identity_hosts=hosts
        )

    assert allowed("GET", "https://app.cern.ch/page")
    assert allowed("OPTIONS", "https://app.cern.ch/api")
    assert not allowed("PUT", "https://app.cern.ch/api/1")
    assert not allowed("PATCH", "https://app.cern.ch/api/1")
    assert not allowed("GET", "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/approve/X")
    token = "https://auth.cern.ch/auth/realms/cern/protocol/openid-connect/token"
    assert allowed("POST", token, "grant_type=refresh_token")
    assert not allowed(
        "POST", "https://evil.cern.ch/auth/realms/cern/protocol/openid-connect/token"
    )
    assert allowed("POST", "https://app.cern.ch/cb", "code=x&state=y", nav=True)
    assert not allowed("POST", "https://app.cern.ch/cb", "code=x&state=y&comment=hi", nav=True)
    assert not allowed("POST", "https://app.cern.ch/acs", "RelayState=x", nav=True)
    assert not allowed("POST", "https://app.cern.ch/search", '{"q": "x"}')

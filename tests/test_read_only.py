"""cmsmcp is read-only: no tool, HTTP path or page render may change remote state."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cmsmcp.app import build_server
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import PolicyViolationError
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.net.safety import browser_request_allowed, check_read_only_post, check_read_only_url
from cmsmcp.policy import READ_EFFECTS
from cmsmcp.registry import CmsMcpServer


async def test_every_registered_tool_is_read_only(tmp_path: Path) -> None:
    server = build_server(Settings(state_dir=tmp_path, require_sso=False))
    tools = await server.list_tools()
    assert len(tools) > 150
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
    assert {info.effect for info in server.tool_infos.values()} <= READ_EFFECTS


def test_registry_refuses_a_non_read_tool(tmp_path: Path) -> None:
    from cmsmcp.policy import Policy  # noqa: PLC0415

    settings = Settings(state_dir=tmp_path, require_sso=False)
    server = CmsMcpServer("t", settings=settings, policy=Policy(settings))
    with pytest.raises(ValueError, match="read-only tools only"):

        @server.tool(name="x_post", effect="act-external")  # type: ignore[arg-type]
        async def x_post() -> str:
            return "never"


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
        # Judged as requested: dot segments are resolved first (literal or encoded).
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/requests/get/../soft_reset/P",
        "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/chained_requests/get/%2e%2e/flow/X",
        "https://cms-docdb.cern.ch/cgi-bin/DocDB/ProcessDocumentAdd/x?y=1",
        "https://cern.service-now.com/logout.do",
        "https://its.cern.ch/jira/secure/Logout!default.jspa",
        "https://twiki.cern.ch/twiki/bin/view/CMS/X?logout=1",
        "https://twiki.cern.ch./twiki/bin/statistics/CMS",
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
        "https://twiki.cern.ch/twiki/bin/view/CMS/./Sub/../Topic",
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
        "https://evil.web.cern.ch/api/now/sp/search",  # right path, wrong host
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
    # Registered read-only POST queries pass, on their own host only.
    assert allowed("POST", "https://cmsrunregistry.web.cern.ch/api/runs_filtered_ordered", "{}")
    assert allowed("POST", "https://xsecdb-xsdb-official.app.cern.ch/api/search", "{}")
    assert not allowed("POST", "https://evil.web.cern.ch/api/search", "{}")
    assert not allowed(
        "POST", "https://its.cern.ch/jira/rest/issueNav/latest/preferredSearchLayout"
    )
    # EDMS GWT RPC: read-named methods only.
    rpc = "https://edms.cern.ch/ui/Edms/utilService"
    read = "7|0|4|https://edms.cern.ch/ui/Edms/|H|cern.edms.ui.client.service.UtilService|getEdmsConfig|1"
    assert allowed("POST", rpc, read)
    assert not allowed("POST", rpc, read.replace("getEdmsConfig", "saveDocument"))
    assert not allowed("POST", rpc, read.replace("getEdmsConfig", "getaway"))
    assert not allowed("POST", "https://edms.cern.ch/ui/Edms/remote_logging", read)
    assert not allowed("POST", rpc, None)


async def test_real_browser_checks_every_redirect_hop(tmp_path: Path) -> None:
    """Chromium follows redirects without routing the next hop; the guard must not."""
    import threading  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from cmsmcp.auth import browser  # noqa: PLC0415

    settings = Settings(state_dir=tmp_path)
    if settings.browser_executable is None:
        pytest.skip("no Chromium/Chrome installed")
    hits: list[str] = []
    page = b"""<p>final</p><img src="/img"><script>
      fetch('/data').then(r => r.json()).then(j => document.body.append('xhr:' + j.v));
    </script>"""
    redirects = {
        "/start": "/hop",
        "/hop": "/page",
        "/evil": "/mcm/restapi/requests/approve/NAV",
        "/img": "/mcm/restapi/requests/approve/IMG",
        "/data": "/data2",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            signed_in = "r=1" in (self.headers.get("Cookie") or "")
            if self.path == "/round" and not signed_in:  # sign-in round trip
                self.send_response(302)
                self.send_header("Location", "/login?next=/round")
                self.end_headers()
                return
            if self.path.startswith("/login"):
                self.send_response(302)
                self.send_header("Set-Cookie", "r=1; Path=/")
                self.send_header("Location", "/round")
                self.end_headers()
                return
            if self.path in redirects:
                self.send_response(302)
                if self.path == "/start":
                    self.send_header("Set-Cookie", "s=1; Path=/")
                self.send_header("Location", redirects[self.path])
                self.end_headers()
                return
            self.send_response(200)
            json_body = self.path == "/data2"
            self.send_header("Content-Type", "application/json" if json_body else "text/html")
            self.end_headers()
            self.wfile.write(b'{"v": 42}' if json_body else page)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    # The rules key on the McM host name; serve "McM" from the local server.
    original = browser.browser_request_allowed

    def as_mcm(method: str, url: str, **kwargs: object) -> bool:
        return original(method, url.replace(base, "https://cms-pdmv-prod.web.cern.ch"), **kwargs)

    try:
        browser.browser_request_allowed = as_mcm  # type: ignore[assignment]
        async with async_playwright() as playwright:
            chromium = await browser._launch_browser(settings, playwright, headless=True)
            context = await chromium.new_context(service_workers="block")
            guard = await browser._install_read_only_guard(context, settings)
            tab = await context.new_page()
            await tab.goto(f"{base}/start", wait_until="domcontentloaded")
            await asyncio.wait_for(guard.settled.wait(), 10)
            await tab.wait_for_load_state("domcontentloaded")
            await tab.wait_for_function("document.body.innerText.includes('xhr:42')")
            assert tab.url == f"{base}/page"
            # A chain that ends on the URL it started at (sign-in) settles too.
            guard.settled.clear()
            await tab.goto(f"{base}/round", wait_until="domcontentloaded")
            await asyncio.wait_for(guard.settled.wait(), 10)
            await tab.wait_for_function("document.body.innerText.includes('final')")
            assert tab.url == f"{base}/round"
            with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
                await tab.goto(f"{base}/evil")
            await chromium.close()
    finally:
        browser.browser_request_allowed = original  # type: ignore[assignment]
        server.shutdown()
    assert not [hit for hit in hits if "approve" in hit], hits
    assert {"/start", "/hop", "/page", "/img", "/data", "/data2"} <= set(hits)

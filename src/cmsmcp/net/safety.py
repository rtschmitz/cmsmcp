"""Read-only request and credential-safe URL policies."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from cmsmcp.contracts.errors import PolicyViolationError

SAFE_METHODS = frozenset({"GET", "HEAD"})
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
SENSITIVE_QUERY_PARTS = (
    "access_token",
    "acs",
    "api_key",
    "apikey",
    "assertion",
    "auth",
    "client_secret",
    "code",
    "credential",
    "idp",
    "jwt",
    "key",
    "password",
    "private_key",
    "saml",
    "secret",
    "session",
    "sig",
    "signature",
    "ticket",
    "token",
)


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """Match exact hosts or exact ``*.example.com`` subdomains."""
    normalized = host.rstrip(".").lower()
    for raw in patterns:
        pattern = raw.rstrip(".").lower()
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if normalized == suffix or normalized.endswith(f".{suffix}"):
                return True
        elif normalized == pattern:
            return True
    return False


def validate_url(url: str, *, allowed_hosts: Iterable[str], require_https: bool = True) -> str:
    """Validate an absolute HTTP(S) URL and return the normalized URL."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise PolicyViolationError("Only absolute HTTP and HTTPS URLs are allowed")
    if require_https and parsed.scheme != "https":
        raise PolicyViolationError("Plain HTTP URLs are not allowed")
    if parsed.username or parsed.password:
        raise PolicyViolationError("Credentials in URLs are not allowed")
    if not parsed.hostname or not host_matches(parsed.hostname, allowed_hosts):
        raise PolicyViolationError(
            f"Host is not in the configured allowlist: {parsed.hostname or 'unknown'}"
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def validate_method(method: str, *, body: object | None = None) -> str:
    """Require a read-only method and an empty request body."""
    normalized = method.upper()
    if normalized not in SAFE_METHODS:
        raise PolicyViolationError(
            f"Only read-only methods are allowed: {', '.join(sorted(SAFE_METHODS))}"
        )
    if body is not None:
        raise PolicyViolationError("Request bodies are not allowed")
    return normalized


def validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Reject client-supplied credential headers and return a plain header mapping."""
    validated = dict(headers or {})
    provided = {name.lower() for name in validated}
    leaked = sorted(provided & SENSITIVE_HEADERS)
    if leaked:
        raise PolicyViolationError(
            f"Credential headers must be supplied by the auth service, not callers: {leaked}"
        )
    return {name: value for name, value in validated.items() if value is not None}


def redact_url(url: str) -> str:
    """Return a URL with likely secret query values replaced."""
    parsed = urlsplit(url)
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        redacted = "<redacted>" if any(part in lowered for part in SENSITIVE_QUERY_PARTS) else value
        pairs.append((key, redacted))
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", maxsplit=1)[1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(pairs), ""))


# ------------------------------------------------------------- read-only URLs
# GET/HEAD alone does not make a request read-only: some CERN services change
# state on a plain GET. McM's REST API approves, resets, clones and deletes
# requests by GET; TWiki and DocDB CGI scripts act on query parameters; a GET
# to a logout endpoint ends the user's session. The HTTP client checks every
# request and redirect hop against the host-specific rules below; ``strict``
# adds generic action-verb rules for URLs an agent chose (generic page and
# file fetches, every request of the headless page browser), where a false
# positive costs one refused fetch.

_LOGOUT_SEGMENTS = frozenset({"logout", "log_out", "log-out", "logoff", "signout", "sign_out"})
_MCM_READ_ACTIONS = frozenset({"get", "output", "produces", "search", "fetch"})
_TWIKI_READ_SCRIPTS = frozenset({"view", "viewauth", "viewfile", "rdiff", "oops"})
_DOCDB_WRITE_MARKERS = (
    "process", "upload", "delete", "clone", "modify", "addfiles", "confirm", "signrevision",
)  # fmt: skip
_METHOD_OVERRIDE_KEYS = frozenset({"_method", "x-http-method-override"})
_ACTION_QUERY_KEYS = frozenset({"action", "do", "op", "cmd", "command", "method", "_action"})
_ACTION_VERBS = frozenset(
    {
        "abort", "accept", "acknowledge", "approve", "cancel", "clone", "create", "decline",
        "delete", "destroy", "follow", "grant", "inject", "invite", "join", "kill", "leave",
        "lock", "publish", "register", "reject", "remove", "rename", "resubmit", "reset",
        "revoke", "rewind", "save", "subscribe", "submit", "toggle", "unfollow", "unlock",
        "unpublish", "unregister", "unsubscribe", "update", "upload", "vote",
    }
)  # fmt: skip


def _resolved_segments(path: str) -> list[str]:
    """Decoded, lower-cased path segments with ``.``/``..`` resolved.

    Rules must judge the path that is actually requested: httpx and browsers
    resolve dot segments before sending, and servers resolve encoded ones
    (``%2e%2e``), so ``get/../approve`` is checked as ``approve``.
    """
    resolved: list[str] = []
    for part in path.split("/"):
        segment = unquote(part).lower()
        if segment == "..":
            if resolved:
                resolved.pop()
        elif segment and segment != ".":
            resolved.append(segment)
    return resolved


def _refuse_state_change(url: str, why: str) -> PolicyViolationError:
    return PolicyViolationError(
        f"Refused {redact_url(url)}: {why}",
        instructions=(
            "cmsmcp is read-only and does not request URLs that can change state on the "
            "server. Use the source's read tools (search/get/list) instead."
        ),
    )


def _service_write_reason(host: str, segments: list[str]) -> str | None:
    """Why a GET to this service path would change state, or None."""
    if _LOGOUT_SEGMENTS.intersection(re.split(r"[.!;]", segment)[0] for segment in segments):
        return "logout endpoint"
    if host.startswith("cms-pdmv") and "restapi" in segments:
        index = segments.index("restapi")
        action = segments[index + 2] if len(segments) > index + 2 else "get"
        if not (action.startswith("get") or action in _MCM_READ_ACTIONS):
            return f"McM REST action {action!r} is not a read"
    if host == "twiki.cern.ch" and segments[:2] == ["twiki", "bin"] and segments[2:]:
        script = re.sub(r"\.(pl|cgi)$", "", segments[2])
        if script not in _TWIKI_READ_SCRIPTS:
            return f"TWiki script {script!r} is not a read"
    if "docdb" in host and "cgi-bin" in segments:
        # Every segment after cgi-bin: CGI PATH_INFO may follow the script name.
        for script in segments[segments.index("cgi-bin") + 1 :]:
            if any(marker in script for marker in _DOCDB_WRITE_MARKERS):
                return f"DocDB script {script!r} is not a read"
    return None


def check_read_only_url(url: str, *, strict: bool = False) -> str:
    """Refuse URLs known to change server state even on GET; return ``url``."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    segments = _resolved_segments(parsed.path)
    query = [(key.lower(), value.lower()) for key, value in parse_qsl(parsed.query)]
    if any(key in _METHOD_OVERRIDE_KEYS and value not in {"get", "head"} for key, value in query):
        raise _refuse_state_change(url, "HTTP method override in the query")
    if any(key in _LOGOUT_SEGMENTS for key, _ in query):
        raise _refuse_state_change(url, "logout parameter")
    if reason := _service_write_reason(host, segments):
        raise _refuse_state_change(url, reason)
    if strict:
        if verbs := _ACTION_VERBS.intersection(segments):
            raise _refuse_state_change(url, f"action path segment {sorted(verbs)[0]!r}")
        if any(key in _ACTION_QUERY_KEYS and value in _ACTION_VERBS for key, value in query):
            raise _refuse_state_change(url, "action verb in the query")
    return url


# ------------------------------------------------------------ read-only POSTs
# Some services only answer queries by POST. These (host, path) pairs are the
# only POST endpoints the HTTP client may call and the page browser may let a
# page call; each was checked to be a pure query. Adding one is a reviewed
# change: it must never change server state.
READ_ONLY_POSTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cern.service-now.com", re.compile(r"/api/now/sp/search")),  # portal search
    ("mattermost.web.cern.ch", re.compile(r"/api/v4/teams/[A-Za-z0-9]+/posts/search")),
    ("cmsrunregistry.web.cern.ch", re.compile(r"/api/(runs|datasets)_filtered_ordered")),
    (
        "cmsrunregistry.web.cern.ch",
        re.compile(r"/api/lumisections/rr_lumisection_ranges(_by_component)?"),
    ),
    ("cmsonline.cern.ch", re.compile(r"/Elog/json\.do")),  # E-Log search (DataTables)
    ("cmsonline.cern.ch", re.compile(r"/webcenter/portal/cmsonline/pages_common/shiftlist")),
    # Loaded by pages rendered with cern_fetch_page:
    ("xsecdb-xsdb-official.app.cern.ch", re.compile(r"/api/search")),  # XSDB table query
    ("its.cern.ch", re.compile(r"/jira/rest/wrm/2\.0/resources")),  # script/style bundles
    ("its.cern.ch", re.compile(r"/jira/rest/issueNav/1/issueTable")),  # issue list query
)
# EDMS's GWT UI sends every call to a few RPC endpoints; the method name in the
# body says what it does, so only read-named methods pass.
_EDMS_RPC_PATH = re.compile(r"/ui/Edms/[A-Za-z]+Service")
_RPC_READ_METHOD = re.compile(r"(get|find|search|list|load|count|is|has)[A-Z]\w*")
_GWT_METHOD_FIELD = 6  # version|flags|count|module base|policy|interface|METHOD|...


def _is_read_only_post(url: str, body: str | None = None) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if any(host == h and pattern.fullmatch(parsed.path) for h, pattern in READ_ONLY_POSTS):
        return True
    if host == "edms.cern.ch" and _EDMS_RPC_PATH.fullmatch(parsed.path) and body:
        fields = body.split("|")
        return len(fields) > _GWT_METHOD_FIELD and bool(
            _RPC_READ_METHOD.fullmatch(fields[_GWT_METHOD_FIELD])
        )
    return False


def check_read_only_post(url: str) -> str:
    """Allow a POST only to a registered read-only query endpoint."""
    if not _is_read_only_post(url):
        raise PolicyViolationError(
            f"POST to {redact_url(url)} is not a registered read-only query endpoint"
        )
    return url


# ------------------------------------------------------------ headless browser
_SAML_FIELDS = frozenset({"samlresponse", "samlrequest", "relaystate", "sigalg", "signature"})
_OIDC_RESPONSE_FIELDS = frozenset(
    {"code", "state", "session_state", "iss", "id_token", "error", "error_description"}
)
_BROWSER_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def browser_request_allowed(
    method: str,
    url: str,
    *,
    post_data: str | None,
    navigation: bool,
    identity_hosts: Iterable[str],
) -> bool:
    """Whether the read-only page browser may send this request.

    Reads (GET/HEAD/OPTIONS) pass unless the URL is a known state change.
    POSTs pass only to registered read-only query endpoints
    (:data:`READ_ONLY_POSTS`, EDMS read-named RPC methods) or as the sign-in
    plumbing a page needs to load: the Keycloak token endpoint (PKCE code
    exchange / refresh) and a navigation that posts back nothing but a SAML or
    OIDC sign-in response. Everything else (form submits, other XHR writes,
    PUT/PATCH/DELETE) is blocked.
    """
    verb = method.upper()
    try:
        check_read_only_url(url, strict=True)
    except PolicyViolationError:
        return False
    if verb in _BROWSER_READ_METHODS:
        return True
    if verb != "POST":
        return False
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    token_endpoint = host in {h.lower() for h in identity_hosts} and parsed.path.endswith(
        "/protocol/openid-connect/token"
    )
    if token_endpoint or _is_read_only_post(url, post_data):
        return True
    if not navigation or not post_data:
        return False
    fields = {key.lower() for key, _ in parse_qsl(post_data, keep_blank_values=True)}
    saml = fields <= _SAML_FIELDS and bool(fields & {"samlresponse", "samlrequest"})
    oidc = fields <= _OIDC_RESPONSE_FIELDS and bool(fields & {"code", "id_token"})
    return saml or oidc

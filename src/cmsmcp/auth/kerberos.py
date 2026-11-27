"""Headless CERN SSO login using a Kerberos ticket plus a terminal-entered 2FA code.

The visible-browser login (:func:`cmsmcp.auth.browser.collect_sso_identity`)
remains the default. This flow replaces only the username/password screen:
Keycloak's ``kerberos`` identity-provider link is followed with a SPNEGO token
generated from the user's ``kinit`` ticket cache, and the 2FA one-time code is
typed into the terminal instead of the browser.

Chrome's built-in Negotiate support is not used: on stock Linux Chrome it can
fail to initialise GSSAPI (``ERR_UNSUPPORTED_AUTH_SCHEME``) even with
``--auth-server-allowlist``. Instead the SPNEGO token is generated with
``python-gssapi`` and attached to exactly one request — the Kerberos realm
authorization URL on the configured SSO identity host. No other request ever
carries it.

The Keycloak identity cookie appears *before* the 2FA step completes, so a
cookie check alone would accept a half-finished login. Success here requires
the browser to land back on the start URL's host with a valid identity cookie.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from cmsmcp.auth.browser import _close_resource, _launch_browser
from cmsmcp.auth.store import SessionRecord
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidSessionError
from cmsmcp.contracts.results import ErrorCode

logger = logging.getLogger(__name__)

# SPNEGO mechanism OID (RFC 4178).
_SPNEGO_OID = "1.3.6.1.5.5.2"
_KERBEROS_LINK = 'a[href*="/broker/kerberos/login"]'
_KERBEROS_REALM_PATH = "/realms/kerberos/"
_OTP_FIELD = "#otp"
_OTP_SUBMIT = "#kc-login"
_OTP_ERROR = "#input-error-otp-code, #input-error, .pf-c-alert__title, .kc-feedback-text"
# Accounts whose default second factor is a security key / passkey land on a
# WebAuthn page first. Keycloak's "Try Another Way" form leads to a credential
# chooser, where the authenticator-app entry brings up the OTP field.
_TRY_ANOTHER_WAY_FORM = "#kc-select-try-another-way-form"
_CREDENTIAL_CHOICE = "#kc-select-credential-form button[name=authenticationExecution]"
_OTP_CHOICE_TEXT = re.compile(r"authenticator app", re.IGNORECASE)
# With several authenticator apps registered, the code form lists them as
# radio buttons and pre-selects one; a code is checked only against the
# selected app, so the user must say which app the code comes from.
_OTP_DEVICE_RADIO = "#kc-otp-login-form input[name=selectedCredentialId]"
_STEP_TIMEOUT_SECONDS = 45
_POLL_SECONDS = 0.5
MAX_OTP_ATTEMPTS = 3

# Receives the previous attempt's error text (None on the first attempt) and
# returns the one-time code to submit.
OtpProvider = Callable[[str | None], Awaitable[str]]
# Receives the authenticator-app labels registered on the account and the index
# of the one to pre-select; returns the index of the app the code will come from.
DeviceChooser = Callable[[list[str], int], Awaitable[int]]
Say = Callable[[str], None]


class KerberosLoginError(CMSMCPError):
    """The Kerberos-backed login could not run or was rejected."""

    code = ErrorCode.AUTH_REQUIRED


def _gssapi() -> Any:
    try:
        import gssapi  # noqa: PLC0415 - optional extra, imported only for this flow
    except ImportError as exc:
        raise KerberosLoginError(
            "Kerberos login needs the optional python-gssapi package",
            instructions=(
                "Install it with `uv sync --extra kerberos` (building it needs the "
                "Kerberos development headers, e.g. `apt install libkrb5-dev`)."
            ),
        ) from exc
    return gssapi


def _no_ticket_error() -> KerberosLoginError:
    return KerberosLoginError(
        "No usable Kerberos ticket was found",
        instructions="Run `kinit <username>@CERN.CH` and retry.",
    )


def check_kerberos_ticket() -> None:
    """Fail fast, before any browser starts, when there is no valid TGT."""
    gssapi = _gssapi()
    try:
        credentials = gssapi.Credentials(usage="initiate")
        lifetime = credentials.lifetime
    except gssapi.exceptions.GSSError as exc:
        raise _no_ticket_error() from exc
    if lifetime is not None and lifetime <= 0:
        raise KerberosLoginError(
            "The Kerberos ticket has expired",
            instructions="Run `kinit -R` (or `kinit <username>@CERN.CH`) and retry.",
        )


def negotiate_header(host: str) -> str:
    """Return an ``Authorization: Negotiate`` value for ``HTTP@host``."""
    gssapi = _gssapi()
    try:
        context = gssapi.SecurityContext(
            name=gssapi.Name(f"HTTP@{host}", gssapi.NameType.hostbased_service),
            usage="initiate",
            mech=gssapi.OID.from_int_seq(_SPNEGO_OID),
        )
        token = context.step()
    except gssapi.exceptions.GSSError as exc:
        raise _no_ticket_error() from exc
    return "Negotiate " + base64.b64encode(token).decode("ascii")


def validate_kerberos_redirect(location: str, identity_host: str) -> str:
    """Only ever send the SPNEGO token to the configured SSO host's Kerberos realm."""
    parts = urlsplit(location)
    if (
        parts.scheme != "https"
        or parts.hostname != identity_host
        or _KERBEROS_REALM_PATH not in parts.path
    ):
        raise KerberosLoginError(
            "The CERN login page redirected Kerberos sign-in somewhere unexpected",
            instructions="Use cern_auth_sign_in (visible browser) instead.",
        )
    return location


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


async def _identity_ready(settings: Settings, context: BrowserContext) -> bool:
    probe = SessionRecord(storage_state={"cookies": await context.cookies(), "origins": []})
    return probe.is_authenticated(
        settings.identity_cookie_names,
        identity_host=settings.sso_identity_host,
        max_session_age_seconds=settings.session_max_age_seconds,
    )


async def _switch_to_otp(
    page: Page, *, tried_another_way: bool, say: Say | None = None
) -> str | None:
    """Steer a security-key/passkey 2FA page towards the authenticator-app code.

    Returns ``chosen`` after picking the authenticator app in the credential
    chooser, ``another_way`` after asking for the chooser, or None when the
    page offers neither. Raises when the account has no authenticator app.
    """
    choices = page.locator(_CREDENTIAL_CHOICE)
    if await choices.count() > 0:
        methods = [
            (await choices.nth(i).inner_text()).strip().splitlines()[0].strip()
            for i in range(await choices.count())
        ]
        otp_choice = choices.filter(has_text=_OTP_CHOICE_TEXT)
        if await otp_choice.count() == 0:
            raise KerberosLoginError(
                f"Your CERN account offers no authenticator app for 2FA ({', '.join(methods)})",
                instructions=(
                    "Terminal login needs a 6-digit authenticator-app code: register one "
                    "in your CERN account settings, or use cern_auth_sign_in (visible browser)."
                ),
            )
        if say is not None:
            say(
                f"2FA methods on your CERN account: {', '.join(methods)}. Using the "
                "authenticator app (security keys and passkeys need a browser)."
            )
        await otp_choice.first.click()
        return "chosen"
    form = page.locator(_TRY_ANOTHER_WAY_FORM)
    if not tried_another_way and await form.count() > 0:
        await form.first.evaluate("f => f.submit()")
        return "another_way"
    return None


async def _wait_for_outcome(
    settings: Settings,
    context: BrowserContext,
    page: Page,
    start_host: str | None,
    say: Say | None = None,
) -> str:
    """Return ``done``, ``otp`` or ``kerberos_rejected`` once the page settles."""
    deadline = asyncio.get_running_loop().time() + _STEP_TIMEOUT_SECONDS
    tried_another_way = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            parts = urlsplit(page.url)
            if parts.hostname == start_host and await _identity_ready(settings, context):
                return "done"
            if parts.hostname == settings.sso_identity_host:
                if _KERBEROS_REALM_PATH in parts.path:
                    return "kerberos_rejected"
                if await page.locator(_OTP_FIELD).count() > 0:
                    return "otp"
                switched = await _switch_to_otp(page, tried_another_way=tried_another_way, say=say)
                if switched == "another_way":
                    tried_another_way = True
                if switched:
                    logger.debug("2FA page is not the code form; switching: %s", switched)
        except PlaywrightError:
            logger.debug("Page mid-navigation while waiting for login outcome", exc_info=True)
        await asyncio.sleep(_POLL_SECONDS)
    raise KerberosLoginError(
        "The CERN login did not reach the 2FA step or finish in time",
        instructions="Retry, or use cern_auth_sign_in (visible browser) instead.",
    )


async def _otp_error_text(page: Page) -> str:
    try:
        locator = page.locator(_OTP_ERROR)
        if await locator.count() > 0:
            text = re.sub(r"\s+", " ", await locator.first.inner_text()).strip()
            if text:
                return text
    except PlaywrightError:
        logger.debug("Could not read the 2FA error text", exc_info=True)
    return "CERN rejected the one-time code"


async def _start_kerberos(settings: Settings, context: BrowserContext, page: Page) -> None:
    """Open the login page and follow its Kerberos link with a SPNEGO token attached."""
    await page.goto(str(settings.sso_start_url), wait_until="domcontentloaded", timeout=30_000)
    link = page.locator(_KERBEROS_LINK)
    await link.first.wait_for(state="attached", timeout=30_000)
    if urlsplit(page.url).hostname != settings.sso_identity_host:
        raise KerberosLoginError(
            "The start URL did not lead to the CERN login page",
            instructions="Check CMSMCP_SSO_START_URL or use cern_auth_sign_in.",
        )
    href = await link.first.get_attribute("href")
    broker_url = urljoin(page.url, href or "")
    validate_kerberos_redirect_host(broker_url, settings.sso_identity_host)

    # The broker step answers 303 → Kerberos realm. Resolve it outside the page
    # so the realm URL is a top-level navigation the route below can match
    # exactly (Playwright does not route redirect hops).
    response = await context.request.get(broker_url, max_redirects=0)
    location = validate_kerberos_redirect(
        urljoin(broker_url, response.headers.get("location", "")),
        settings.sso_identity_host,
    )
    target = _strip_fragment(location)
    header = negotiate_header(settings.sso_identity_host)

    async def attach_token(route: Any) -> None:
        headers = await route.request.all_headers()
        headers["authorization"] = header
        await route.continue_(headers=headers)

    def matches(url: str) -> bool:
        return _strip_fragment(url) == target

    await page.route(matches, attach_token, times=1)
    try:
        await page.goto(location, wait_until="domcontentloaded", timeout=30_000)
    finally:
        await page.unroute(matches)


def validate_kerberos_redirect_host(url: str, identity_host: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != identity_host:
        raise KerberosLoginError(
            "The CERN login page's Kerberos link points somewhere unexpected",
            instructions="Use cern_auth_sign_in (visible browser) instead.",
        )


async def _otp_devices(page: Page) -> tuple[list[str], int]:
    """Authenticator-app labels on the code form and the index CERN pre-selected."""
    radios = page.locator(_OTP_DEVICE_RADIO)
    names: list[str] = []
    selected = 0
    for index in range(await radios.count()):
        radio = radios.nth(index)
        radio_id = await radio.get_attribute("id")
        label = page.locator(f'label[for="{radio_id}"]') if radio_id else None
        text = (await label.first.inner_text()).strip() if label and await label.count() else ""
        names.append(text or f"authenticator {index + 1}")
        if await radio.is_checked():
            selected = index
    return names, selected


async def _select_otp_device(
    page: Page, chooser: DeviceChooser | None, previous: str | None
) -> str | None:
    """Select the authenticator app the code will come from; returns its label.

    Only accounts with several apps get a choice. On a retry the previously
    chosen app is offered as the default, so a wrong pick can be corrected.
    """
    names, default = await _otp_devices(page)
    if len(names) < 2:  # noqa: PLR2004 - nothing to choose
        return previous
    if previous in names:
        default = names.index(previous)
    index = default if chooser is None else await chooser(names, default)
    if not 0 <= index < len(names):
        raise KerberosLoginError(f"No authenticator app number {index + 1}")
    await page.locator(_OTP_DEVICE_RADIO).nth(index).check()
    return names[index]


async def _complete_otp(
    settings: Settings,
    context: BrowserContext,
    page: Page,
    start_host: str | None,
    otp_provider: OtpProvider,
    *,
    device_chooser: DeviceChooser | None = None,
) -> None:
    previous_error: str | None = None
    device: str | None = None
    for _ in range(MAX_OTP_ATTEMPTS):
        device = await _select_otp_device(page, device_chooser, device)
        code = await otp_provider(previous_error)
        await page.fill(_OTP_FIELD, code)
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            await page.click(_OTP_SUBMIT)
        outcome = await _wait_for_outcome(settings, context, page, start_host)
        if outcome == "done":
            return
        if outcome != "otp":
            break
        previous_error = await _otp_error_text(page)
    raise InvalidSessionError(
        "CERN did not accept the one-time code",
        instructions="Run the login again with a fresh code from your authenticator app.",
    )


def make_kerberos_collector(
    otp_provider: OtpProvider,
    device_chooser: DeviceChooser | None = None,
    say: Say | None = None,
) -> Callable[[Settings, int | None], Awaitable[dict[str, Any]]]:
    """Build an :class:`AuthService` storage-state collector for this flow.

    ``device_chooser`` picks the authenticator app when several are registered
    (None keeps CERN's pre-selection); ``say`` receives progress notes for the
    terminal user.
    """

    async def collect(settings: Settings, _timeout_seconds: int | None) -> dict[str, Any]:
        check_kerberos_ticket()
        start_host = urlsplit(str(settings.sso_start_url)).hostname
        async with async_playwright() as playwright:
            browser = await _launch_browser(settings, playwright, headless=True)
            context: BrowserContext | None = None
            try:
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
                await _start_kerberos(settings, context, page)
                outcome = await _wait_for_outcome(settings, context, page, start_host, say)
                if outcome == "kerberos_rejected":
                    raise KerberosLoginError(
                        "CERN did not accept the Kerberos ticket",
                        instructions=(
                            "Check `klist` shows a valid CERN.CH ticket, run `kinit` "
                            "again if needed, and retry."
                        ),
                    )
                if outcome == "otp":
                    await _complete_otp(
                        settings,
                        context,
                        page,
                        start_host,
                        otp_provider,
                        device_chooser=device_chooser,
                    )
                return await context.storage_state()
            finally:
                await _close_resource(context, "kerberos context")
                await _close_resource(browser, "kerberos browser")

    return collect

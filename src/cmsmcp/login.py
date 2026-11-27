"""``cmsmcp login``: sign in to CERN SSO from the terminal with Kerberos + 2FA.

Uses the ``kinit`` ticket instead of the username/password screen and asks for
the 2FA one-time code in the terminal (or takes it from ``--otp``). The session
lands in the same store the MCP server reads, so a running server picks it up
on its next tool call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime

from cmsmcp.auth.kerberos import (
    DeviceChooser,
    KerberosLoginError,
    OtpProvider,
    make_kerberos_collector,
)
from cmsmcp.auth.service import AuthService, StorageStateCollector
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import CMSMCPError, InvalidSessionError

_CODE_PATTERN = re.compile(r"\d{6,8}")
# Default authenticator app (part of its CERN label) when --device is not given.
DEVICE_ENV = "CMSMCP_OTP_DEVICE"


def normalise_code(raw: str) -> str:
    """Strip spaces from a one-time code and check it looks like one."""
    code = re.sub(r"\s+", "", raw)
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError("a one-time code is 6-8 digits")
    return code


def make_otp_provider(
    otp: str | None,
    *,
    interactive: bool,
    prompt: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> OtpProvider:
    """Return the code from ``--otp`` once, or prompt in the terminal."""
    used = False

    async def provide(previous_error: str | None) -> str:
        nonlocal used
        if otp is not None:
            if used:
                raise InvalidSessionError(
                    "CERN did not accept the one-time code passed with --otp",
                    instructions="Run `cmsmcp login` again with a fresh code.",
                )
            used = True
            return normalise_code(otp)
        if not interactive:
            raise KerberosLoginError(
                "CERN is asking for a 2FA code but there is no terminal to type it into",
                instructions="Pass the code with `cmsmcp login --otp <code>`.",
            )
        if previous_error:
            say(f"{previous_error}. Try again.")
        while True:
            raw = await asyncio.to_thread(prompt, "CERN 2FA code: ")
            try:
                return normalise_code(raw)
            except ValueError:
                say("Enter the 6-digit code from your authenticator app.")

    return provide


def pick_device(answer: str, names: Sequence[str], default: int) -> int | None:
    """Resolve a typed choice: empty = default, a 1-based number, or part of a name."""
    answer = answer.strip()
    if not answer:
        return default
    if answer.isdigit():
        index = int(answer) - 1
        return index if 0 <= index < len(names) else None
    matches = [i for i, name in enumerate(names) if answer.lower() in name.lower()]
    return matches[0] if len(matches) == 1 else None


def make_device_chooser(
    wanted: str | None,
    *,
    interactive: bool,
    prompt: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> DeviceChooser:
    """Ask which authenticator app the code comes from (accounts with several).

    ``wanted`` (``--device`` / ``CMSMCP_OTP_DEVICE``) answers without a prompt;
    otherwise the terminal lists the apps; without a terminal, CERN's
    pre-selection is kept and named.
    """

    async def choose(names: list[str], default: int) -> int:
        if wanted:
            index = pick_device(wanted, names, default)
            if index is None:
                raise KerberosLoginError(
                    f"Authenticator app {wanted!r} does not match exactly one of yours",
                    instructions=f"Registered on your CERN account: {', '.join(names)}.",
                )
        elif not interactive:
            index = default
        else:
            say("Authenticator apps registered on your CERN account:")
            for number, name in enumerate(names, start=1):
                say(f"  {number}) {name}")
            while True:
                raw = await asyncio.to_thread(
                    prompt, f"Which app will you take the code from? [{default + 1}]: "
                )
                index = pick_device(raw, names, default)
                if index is not None:
                    break
                say(f"Enter a number from 1 to {len(names)}, or part of an app name.")
        say(f"Code from: {names[index]}" + ("" if interactive else " (choose with --device)"))
        return index

    return choose


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmsmcp login",
        description=(
            "Sign in to CERN SSO without a browser window: your Kerberos ticket "
            "(from kinit) replaces the password, and the 2FA code is entered here."
        ),
    )
    parser.add_argument(
        "--otp",
        metavar="CODE",
        help="2FA one-time code; use when there is no interactive terminal",
    )
    parser.add_argument(
        "--device",
        metavar="NAME",
        help=(
            "authenticator app the code comes from, when several are registered "
            f"(number or part of its name; default: ask, or ${DEVICE_ENV})"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="discard the current session and sign in again even if it is still valid",
    )
    return parser


def run(
    argv: Sequence[str],
    *,
    settings: Settings | None = None,
    interactive: bool | None = None,
    collector_factory: Callable[..., StorageStateCollector] = make_kerberos_collector,
) -> int:
    """Run the login command; returns a process exit code."""
    args = _parser().parse_args(list(argv))
    if args.otp is not None:
        try:
            normalise_code(args.otp)
        except ValueError:
            print("--otp must be the 6-8 digit code from your authenticator app.", file=sys.stderr)
            return 2
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or Settings()
    interactive = sys.stdin.isatty() if interactive is None else interactive
    provider = make_otp_provider(args.otp, interactive=interactive)
    chooser = make_device_chooser(
        args.device or os.environ.get(DEVICE_ENV) or None, interactive=interactive
    )
    auth = AuthService(
        settings, collector=collector_factory(provider, device_chooser=chooser, say=print)
    )
    try:
        status = auth.status()
        if status.authenticated and not args.force:
            print(
                f"Already signed in to CERN SSO (valid until {_local(status.expires_at)}). "
                "Use --force to sign in again."
            )
            return 0
        result = asyncio.run(auth.reauthenticate() if args.force else auth.sign_in())
    except CMSMCPError as exc:
        print(f"Login failed: {exc.message}", file=sys.stderr)
        if exc.instructions:
            print(exc.instructions, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    print(f"Signed in to CERN SSO. Session valid until {_local(result.expires_at)}.")
    return 0


def _local(moment: datetime | None) -> str:
    if moment is None:
        return "an unknown time"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")

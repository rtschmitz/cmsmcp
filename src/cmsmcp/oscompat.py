"""Best-effort macOS/Windows accommodations. Linux is the only supported platform.

Every helper here returns exactly the Linux behaviour on Linux; the branches
for macOS (``IS_MACOS``) and Windows (``IS_WINDOWS``) only paper over the
differences the rest of the code base would otherwise trip on:

- config/state roots: ``%APPDATA%``/``%LOCALAPPDATA%`` on Windows (an explicit
  ``XDG_*`` variable still wins everywhere);
- private-file checks: Windows has no POSIX mode bits (``stat`` reports 0o666),
  so files are protected by the per-user profile ACLs instead;
- browser discovery: Chrome/Chromium/Edge app-bundle and Program Files paths;
- TLS trust: certifi plus the bundled CERN root/grid CAs (``cern_ca.pem``),
  because these Pythons do not see the CERN Grid CA that signs e.g.
  cms-docdb.cern.ch and cmsweb.cern.ch;
- grid proxy default path without ``os.getuid`` on Windows;
- MIT Kerberos tools on Windows (never the unrelated ``System32\\klist.exe``);
- lxplus: Windows OpenSSH lacks ControlMaster, which ``cmsmcp connect``
  depends on, so lxplus tools point the user to WSL;
- subprocess environments, atomic replaces and download names that Windows
  needs adjusted.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Linux behaviour is the reference; anything else is best effort.
UNSUPPORTED_PLATFORM = "Windows" if IS_WINDOWS else "macOS" if IS_MACOS else None

LXPLUS_ON_WINDOWS = (
    "lxplus tools are not available on native Windows: they reuse an OpenSSH "
    "ControlMaster connection (opened once with your 2FA code), which Windows "
    "OpenSSH does not support. Run cmsmcp inside WSL for lxplus/EOS/HTCondor."
)

_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REPLACE_ATTEMPTS = 10
_REPLACE_BACKOFF_SECONDS = 0.05


def config_root() -> Path:
    """Parent directory of the ``cmsmcp`` config directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    if IS_WINDOWS and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"])
    return Path.home() / ".config"


def state_root() -> Path:
    """Parent directory of the ``cmsmcp`` state directory."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg)
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"])
    return Path.home() / ".local" / "state"


def shared_with_others(mode: int) -> bool:
    """Whether ``st_mode`` grants group/other access (never on Windows: no POSIX bits)."""
    if IS_WINDOWS:
        return False
    return bool(mode & 0o077)


def browser_candidates() -> list[str]:
    """Absolute browser paths to try when none is on PATH (empty on Linux)."""
    if IS_MACOS:
        apps = [
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
        roots = [Path("/Applications"), Path.home() / "Applications"]
        return [str(root / app) for app in apps for root in roots]
    if IS_WINDOWS:
        apps = [
            ("Google", "Chrome", "Application", "chrome.exe"),
            ("Chromium", "Application", "chrome.exe"),
            ("Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        roots = [
            os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        return [str(Path(root, *app)) for app in apps for root in roots if root]
    return []


def find_browser(path_names: tuple[str, ...]) -> str | None:
    for name in path_names:
        path = shutil.which(name)
        if path:
            return path
    for candidate in browser_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


def default_proxy_path() -> Path:
    """``$X509_USER_PROXY`` or the path ``voms-proxy-init`` writes by default."""
    explicit = os.environ.get("X509_USER_PROXY")
    if explicit:
        return Path(explicit)
    if hasattr(os, "getuid"):
        return Path(f"/tmp/x509up_u{os.getuid()}")  # noqa: S108 - voms-proxy-init default
    return Path(tempfile.gettempdir()) / f"x509up_u{getpass.getuser()}"


def kerberos_tool(name: str) -> str | None:
    """Path of ``kinit``/``klist``; on Windows only MIT Kerberos for Windows counts."""
    if not IS_WINDOWS:
        return shutil.which(name)
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if root:
            candidate = Path(root) / "MIT" / "Kerberos" / "bin" / f"{name}.exe"
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows").lower()
    # System32\klist.exe lists Windows (SSPI) tickets and has no ``-s``.
    if found and not found.lower().startswith(system_root):
        return found
    return None


def kerberos_command(name: str) -> str:
    """argv[0] for running ``kinit``: the bare name, or the MIT path on Windows."""
    return (kerberos_tool(name) or name) if IS_WINDOWS else name


def kerberos_install_hint(linux_hint: str) -> str:
    """How to install the Kerberos client here (``linux_hint`` off Windows)."""
    if IS_WINDOWS:
        return "install MIT Kerberos for Windows: https://web.mit.edu/kerberos/dist/"
    return linux_hint


def kerberos_extra_hint(linux_hint: str) -> str:
    """What installing the ``kerberos`` extra (python-gssapi) needs here."""
    if IS_WINDOWS:
        return "prebuilt wheel; needs MIT Kerberos for Windows installed"
    if IS_MACOS:
        return "prebuilt wheel"
    return linux_hint


def lxplus_unsupported() -> str | None:
    """Why lxplus SSH tools cannot run on this platform, or None."""
    return LXPLUS_ON_WINDOWS if IS_WINDOWS else None


def trust_context() -> ssl.SSLContext:
    """TLS verification context for all outbound HTTPS.

    Linux: the system CA store, unchanged. macOS/Windows: the system store plus
    certifi plus the bundled CERN root/grid CAs.
    """
    context = ssl.create_default_context()
    if UNSUPPORTED_PLATFORM is None:
        return context
    import certifi  # noqa: PLC0415 - httpx dependency, only needed off Linux

    context.load_verify_locations(cafile=certifi.where())
    bundle = resources.files("cmsmcp").joinpath("cern_ca.pem")
    context.load_verify_locations(cadata=bundle.read_text(encoding="ascii"))
    return context


def extra_subprocess_env() -> dict[str, str]:
    """Variables a minimal subprocess environment needs on this platform."""
    if not IS_WINDOWS:
        return {}
    names = ("SYSTEMROOT", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
    return {name: os.environ[name] for name in names if name in os.environ}


def replace_file(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """``os.replace``; on Windows retried briefly while another process holds ``target``."""
    if not IS_WINDOWS:
        os.replace(source, target)
        return
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def safe_filename(name: str) -> str:
    """A download filename the local filesystem accepts (unchanged off Windows).

    On Windows ``:`` would address an NTFS alternate data stream, and device
    names (``CON``, ``NUL``...) or trailing dots/spaces misbehave.
    """
    if not IS_WINDOWS:
        return name
    cleaned = _WINDOWS_BAD_CHARS.sub("_", name).rstrip(" .") or "download"
    return f"_{cleaned}" if _WINDOWS_RESERVED.match(cleaned) else cleaned

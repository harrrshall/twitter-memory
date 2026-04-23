"""Pure detection helpers — OS, Python version, Chromium-based browsers,
free ports. Stdlib-only, no side effects."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OSInfo:
    kind: str           # "linux" | "macos"
    distro: str | None  # e.g. "Ubuntu 24.04" or None
    arch: str           # "x86_64" | "arm64"


def detect_os() -> OSInfo:
    system = platform.system()
    if system == "Darwin":
        return OSInfo(kind="macos", distro=None, arch=platform.machine())
    if system == "Linux":
        distro = None
        os_release = Path("/etc/os-release")
        if os_release.is_file():
            fields: dict[str, str] = {}
            for ln in os_release.read_text().splitlines():
                if "=" in ln:
                    k, _, v = ln.partition("=")
                    fields[k] = v.strip().strip('"')
            name = fields.get("PRETTY_NAME") or fields.get("NAME")
            if name:
                distro = name
        return OSInfo(kind="linux", distro=distro, arch=platform.machine())
    raise RuntimeError(f"Unsupported OS: {system}. Only Linux and macOS are supported.")


@dataclass(frozen=True)
class PythonInfo:
    executable: str
    version: tuple[int, int, int]

    @property
    def version_str(self) -> str:
        return ".".join(str(n) for n in self.version)


def detect_python(min_version: tuple[int, int] = (3, 11)) -> PythonInfo:
    info = PythonInfo(
        executable=sys.executable,
        version=sys.version_info[:3],
    )
    if info.version[:2] < min_version:
        raise RuntimeError(
            f"Found Python {info.version_str} at {info.executable}. "
            f"Need >={'.'.join(str(n) for n in min_version)}. Install: "
            "macOS -> brew install python@3.11 ; "
            "Linux -> pyenv or your distro python3.11 package. "
            "Then re-run: PYTHON=/path/to/python3.11 ./install.sh"
        )
    return info


# Browser paths by OS. Order = preference (user's likely default first).
_CHROMIUM_COMMANDS_LINUX = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge", "microsoft-edge-stable",
    "vivaldi", "vivaldi-stable", "opera",
)
_CHROMIUM_APPS_MACOS = (
    "Google Chrome", "Chromium", "Brave Browser", "Arc",
    "Microsoft Edge", "Vivaldi", "Opera",
)


@dataclass(frozen=True)
class BrowserInfo:
    chromium_found: list[str]
    firefox_only: bool   # True if Firefox detected but no Chromium browser


def detect_browsers(os_info: OSInfo) -> BrowserInfo:
    found: list[str] = []
    if os_info.kind == "linux":
        for cmd in _CHROMIUM_COMMANDS_LINUX:
            if shutil.which(cmd):
                found.append(cmd)
        firefox_present = bool(shutil.which("firefox") or shutil.which("firefox-esr"))
    else:  # macos
        for app in _CHROMIUM_APPS_MACOS:
            if Path(f"/Applications/{app}.app").is_dir():
                found.append(app)
        firefox_present = Path("/Applications/Firefox.app").is_dir()
    return BrowserInfo(
        chromium_found=found,
        firefox_only=bool(firefox_present and not found),
    )


# -- port probing -----------------------------------------------------------


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if binding to 127.0.0.1:<port> fails. Uses SO_REUSEADDR the same
    way uvicorn does so the probe doesn't false-positive because of a
    recently-closed socket in TIME_WAIT."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError:
        return True
    finally:
        s.close()
    return False


def find_free_port(
    preferred: int = 8765,
    fallback_range: tuple[int, int] = (8766, 8775),
) -> int:
    """Return the preferred port if free; otherwise the first free port in
    the fallback range. Raises RuntimeError if nothing in the range is free."""
    if not port_in_use(preferred):
        return preferred
    lo, hi = fallback_range
    for p in range(lo, hi + 1):
        if not port_in_use(p):
            return p
    raise RuntimeError(
        f"No free port in {preferred} or {lo}-{hi}. "
        "Stop whatever is binding these ports and re-run."
    )


# -- env file --------------------------------------------------------------


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-sourceable KEY=VALUE file. Lines starting with # are
    comments. Blank lines ignored. No quoting, no variable expansion."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        out[k.strip()] = v.strip()
    return out


def render_env_file(values: dict[str, str]) -> str:
    lines = [
        "# twitter-memory environment — sourced by the systemd/launchd service.",
        "# Edit values here; re-run ./install.sh to propagate into the service.",
        "",
    ]
    for k in sorted(values):
        lines.append(f"{k}={values[k]}")
    lines.append("")
    return "\n".join(lines)


# -- user environment ------------------------------------------------------


def home() -> Path:
    return Path(os.path.expanduser("~"))


def username() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

"""launchd LaunchAgent install + verify for macOS.

Generates the plist via ``plistlib.dumps()`` from a dict — avoids XML-
escape footguns that a template-string approach would carry.
"""
from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.install_helpers import detect
from scripts.install_helpers.runner import Runner

LABEL = "com.local.twitter-memory"
PLIST_NAME = f"{LABEL}.plist"


@dataclass
class MacServiceConfig:
    repo: Path
    venv_python: Path
    env_file: Path          # ~/.twitter-memory/env
    plist_dir: Path         # ~/Library/LaunchAgents
    logs_dir: Path          # ~/Library/Logs/twitter-memory

    @property
    def plist_path(self) -> Path:
        return self.plist_dir / PLIST_NAME


def render_plist(cfg: MacServiceConfig) -> bytes:
    env_vars = detect.parse_env_file(cfg.env_file)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(cfg.venv_python), "-m", "backend.main"],
        "WorkingDirectory": str(cfg.repo),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        # Restart on crash, but don't loop after a clean exit or a user
        # `launchctl unload`. Plain `KeepAlive: True` would fight uninstall.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(cfg.logs_dir / "stdout.log"),
        "StandardErrorPath": str(cfg.logs_dir / "stderr.log"),
        # ProcessType Interactive lets the agent use graphical services if
        # ever needed. Background would also work; either is fine.
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(plist)


def launchctl_available() -> bool:
    return bool(subprocess.run(
        ["which", "launchctl"],
        capture_output=True, text=True, check=False,
    ).stdout.strip())


def install(cfg: MacServiceConfig, runner: Runner) -> None:
    if not launchctl_available():
        print("launchctl not found; skipping autostart setup. Run manually:")
        print(f"  {cfg.venv_python} -m backend.main")
        return
    runner.mkdir(cfg.plist_dir)
    runner.mkdir(cfg.logs_dir)
    content = render_plist(cfg).decode("utf-8")
    runner.write(cfg.plist_path, content, mode=0o644)

    # Unload first to avoid "already loaded" — ignore failures.
    runner.run(
        ["launchctl", "unload", "-w", str(cfg.plist_path)],
        check=False,
    )
    runner.run(["launchctl", "load", "-w", str(cfg.plist_path)])


def uninstall(cfg: MacServiceConfig, runner: Runner) -> None:
    if cfg.plist_path.exists():
        runner.run(["launchctl", "unload", "-w", str(cfg.plist_path)], check=False)
    runner.remove(cfg.plist_path)


def is_active() -> bool:
    try:
        proc = subprocess.run(
            ["launchctl", "list", LABEL],
            capture_output=True, text=True, check=False, timeout=3,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def dump_recent_log(cfg: MacServiceConfig, n_lines: int = 50) -> str:
    stderr_log = cfg.logs_dir / "stderr.log"
    if not stderr_log.is_file():
        return f"(no log at {stderr_log})"
    try:
        proc = subprocess.run(
            ["tail", "-n", str(n_lines), str(stderr_log)],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return proc.stdout or "(empty)"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"(tail unavailable: {e})"

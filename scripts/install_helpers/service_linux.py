"""systemd --user service install + verify for Linux."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.install_helpers import detect
from scripts.install_helpers.runner import Runner

SERVICE_NAME = "twitter-memory.service"


@dataclass
class LinuxServiceConfig:
    repo: Path
    venv_python: Path
    env_file: Path       # e.g. ~/.twitter-memory/env
    unit_dir: Path       # ~/.config/systemd/user
    template: Path       # scripts/systemd/twitter-memory.service.template

    @property
    def unit_path(self) -> Path:
        return self.unit_dir / SERVICE_NAME


def render_unit(cfg: LinuxServiceConfig) -> str:
    template = cfg.template.read_text(encoding="utf-8")
    return (
        template
        .replace("{{REPO}}", str(cfg.repo))
        .replace("{{VENV_PYTHON}}", str(cfg.venv_python))
        .replace("{{ENV_FILE}}", str(cfg.env_file))
    )


def systemctl_available() -> bool:
    """True if `systemctl --user` works (user-bus available).

    WSL1 and some minimal distros don't have user-session systemd — we
    detect and fall back to "manual-run" guidance in that case.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "status", "--no-pager"],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    # Any response, even "inactive" or non-zero exit, means the user bus is up.
    # The failure we care about is "Failed to connect to bus" / missing binary.
    stderr = proc.stderr.lower()
    return "failed to connect" not in stderr and "no such file" not in stderr


def install(cfg: LinuxServiceConfig, runner: Runner, *, enable_linger: bool) -> None:
    """Render + install the unit. Caller has already decided linger via prompt."""
    if not systemctl_available():
        print(
            "\nsystemctl --user is unavailable on this system (WSL1 / minimal "
            "distro?). Skipping autostart setup — the rest of install continues. "
            "To run the backend manually:\n"
            f"  {cfg.venv_python} -m backend.main\n"
        )
        return

    unit_content = render_unit(cfg)
    runner.mkdir(cfg.unit_dir)
    runner.write(cfg.unit_path, unit_content, mode=0o644)
    runner.run(["systemctl", "--user", "daemon-reload"])
    runner.run(["systemctl", "--user", "enable", SERVICE_NAME])
    runner.run(["systemctl", "--user", "restart", SERVICE_NAME])

    if enable_linger:
        user = detect.username()
        # enable-linger fails without privileges on some distros; don't abort.
        runner.run(["loginctl", "enable-linger", user], check=False)


def uninstall(cfg: LinuxServiceConfig, runner: Runner) -> None:
    if not systemctl_available():
        print("systemctl --user unavailable; nothing to uninstall on service side.")
        return
    runner.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False)
    runner.remove(cfg.unit_path)
    runner.run(["systemctl", "--user", "daemon-reload"], check=False)


def is_active(cfg: LinuxServiceConfig) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True, check=False, timeout=3,
        )
        return proc.stdout.strip() == "active"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def dump_recent_log(n_lines: int = 50) -> str:
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE_NAME, "-n", str(n_lines), "--no-pager"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        return proc.stdout or proc.stderr or "(no log output)"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"(journalctl unavailable: {e})"

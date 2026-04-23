"""One-click installer orchestrator for twitter-memory.

Invoked via ``install.sh`` after ``.venv`` and dependencies exist. Owns
data dir + env file creation, service install (systemd/launchd), Claude
Desktop config merge, health-check verification, extension nudge, and
idempotency manifest.

Usage::

    install.sh [--dry-run] [--port N] [--skip-claude] [--skip-extension]
               [--autostart-only] [--force] [--yes]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from scripts.install_helpers import (
    detect,
    merge_claude_config,
    service_linux,
    service_macos,
)
from scripts.install_helpers.runner import Runner

SCHEMA_VERSION = "v3"


# --------------------------------------------------------------------------

@dataclass
class Env:
    repo: Path
    venv_python: Path
    requirements: Path
    data_dir: Path
    env_file: Path
    manifest_path: Path
    os_info: detect.OSInfo
    python_info: detect.PythonInfo
    browser_info: detect.BrowserInfo
    port: int
    interactive: bool = True

    # Populated lazily
    systemd_cfg: service_linux.LinuxServiceConfig | None = None
    launchd_cfg: service_macos.MacServiceConfig | None = None


# --------------------------------------------------------------------------

def _prompt_yn(question: str, default_yes: bool, interactive: bool) -> bool:
    if not interactive:
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{question} {suffix} ").strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------

def preflight(args: argparse.Namespace) -> Env:
    repo = Path(__file__).resolve().parents[2]
    venv_python = repo / ".venv" / "bin" / "python"
    requirements = repo / "requirements.txt"

    if not venv_python.exists():
        raise SystemExit(
            f"Expected .venv at {venv_python} — install.sh should have created it. "
            "If you invoked installer.py directly, run ./install.sh instead."
        )

    os_info = detect.detect_os()
    python_info = detect.detect_python(min_version=(3, 11))
    browser_info = detect.detect_browsers(os_info)

    data_dir = Path(
        os.environ.get("TWITTER_MEMORY_DATA")
        or (detect.home() / ".twitter-memory")
    )
    env_file = data_dir / "env"
    manifest_path = data_dir / ".install-manifest.json"

    # Port selection: --port wins; else prefer existing env file value; else probe.
    if args.port:
        port = args.port
    else:
        existing_env = detect.parse_env_file(env_file) if env_file.is_file() else {}
        preferred = int(existing_env.get("TWITTER_MEMORY_PORT", "8765"))
        if detect.port_in_use(preferred):
            # If the existing port is in use and it's already ours, keep it — the
            # service is (presumably) running. Otherwise probe a free one.
            manifest = _read_manifest(manifest_path)
            if manifest and manifest.get("port") == preferred:
                port = preferred
            else:
                free = detect.find_free_port(preferred)
                interactive = args.interactive
                if preferred != free and _prompt_yn(
                    f"Port {preferred} is in use. Use {free} instead?",
                    default_yes=True,
                    interactive=interactive,
                ):
                    port = free
                else:
                    port = preferred  # user will have to free it manually
        else:
            port = preferred

    return Env(
        repo=repo,
        venv_python=venv_python,
        requirements=requirements,
        data_dir=data_dir,
        env_file=env_file,
        manifest_path=manifest_path,
        os_info=os_info,
        python_info=python_info,
        browser_info=browser_info,
        port=port,
        interactive=args.interactive,
    )


def _read_manifest(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ensure_data_dir(env: Env, runner: Runner) -> None:
    runner.mkdir(env.data_dir)
    values = {
        "TWITTER_MEMORY_DATA": str(env.data_dir),
        "TWITTER_MEMORY_PORT": str(env.port),
    }
    existing = detect.parse_env_file(env.env_file)
    # Preserve user-added keys like TWITTER_MEMORY_TZ.
    merged = {**existing, **values}
    runner.write(env.env_file, detect.render_env_file(merged), mode=0o600)


def install_service(env: Env, runner: Runner, args: argparse.Namespace) -> str:
    """Returns "systemd" | "launchd" | "none"."""
    if env.os_info.kind == "linux":
        cfg = service_linux.LinuxServiceConfig(
            repo=env.repo,
            venv_python=env.venv_python,
            env_file=env.env_file,
            unit_dir=detect.home() / ".config" / "systemd" / "user",
            template=env.repo / "scripts" / "systemd" / "twitter-memory.service.template",
        )
        env.systemd_cfg = cfg
        if not service_linux.systemctl_available():
            service_linux.install(cfg, runner, enable_linger=False)  # prints the WSL/minimal note
            return "none"
        linger = _prompt_yn(
            "Enable linger so the backend survives logout (recommended only for "
            "always-on machines, skip for laptops)?",
            default_yes=False,
            interactive=env.interactive,
        )
        service_linux.install(cfg, runner, enable_linger=linger)
        return "systemd"

    # macOS
    cfg = service_macos.MacServiceConfig(
        repo=env.repo,
        venv_python=env.venv_python,
        env_file=env.env_file,
        plist_dir=detect.home() / "Library" / "LaunchAgents",
        logs_dir=detect.home() / "Library" / "Logs" / "twitter-memory",
    )
    env.launchd_cfg = cfg
    service_macos.install(cfg, runner)
    return "launchd" if service_macos.launchctl_available() else "none"


def merge_claude(env: Env, runner: Runner, args: argparse.Namespace) -> str:
    """Returns the resulting merge status string."""
    if runner.dry_run:
        # Still resolve + diff via Result; run in-process.
        config_path = merge_claude_config.default_config_path()
        result = merge_claude_config.check(
            config_path,
            merge_claude_config.desired_entry(str(env.venv_python), str(env.repo)),
        )
        print(f"[dry-run] claude config at {config_path} — status: {result.status}")
        return result.status

    cmd = [
        str(env.venv_python), "-m", "scripts.install_helpers.merge_claude_config",
        "--add",
        "--venv-python", str(env.venv_python),
        "--repo", str(env.repo),
    ]
    if args.force:
        cmd.append("--force")
    if not env.interactive:
        cmd.append("--yes")
    proc = subprocess.run(cmd, cwd=str(env.repo), text=True)
    if proc.returncode == 0:
        return "ok"
    if proc.returncode == 1:
        return "conflict"
    if proc.returncode == 2:
        return "malformed"
    return f"exit-{proc.returncode}"


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def wait_for_health(env: Env, runner: Runner, *, timeout: float = 30.0) -> bool:
    if runner.dry_run:
        print(f"[dry-run] skipping health check (would GET http://127.0.0.1:{env.port}/health)")
        return True

    url = f"http://127.0.0.1:{env.port}/health"
    waits = [0.5, 1.0, 2.0, 3.0, 5.0, 5.0, 5.0, 5.0]
    elapsed = 0.0
    for wait in waits:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    print(f"  ok: {url} responded {resp.status}")
                    return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(wait)
        elapsed += wait
        if elapsed >= timeout:
            break

    print(f"\n  FAIL: {url} did not respond within {timeout:.0f}s. Recent log:\n")
    if env.os_info.kind == "linux":
        print(service_linux.dump_recent_log())
    else:
        assert env.launchd_cfg is not None
        print(service_macos.dump_recent_log(env.launchd_cfg))
    return False


# --------------------------------------------------------------------------
# Extension nudge
# --------------------------------------------------------------------------

def extension_nudge(env: Env) -> None:
    ext_dir = env.repo / "extension"
    print()
    print("== One manual step left: the Chrome extension ==")
    print()
    if env.browser_info.firefox_only:
        print("Only Firefox was detected. The twitter-memory extension is MV3 and")
        print("needs a Chromium-based browser (Chrome, Chromium, Brave, Edge, Arc,")
        print("or Vivaldi). Install one, then come back to this step.")
        print()
    elif not env.browser_info.chromium_found:
        print("No browser detected. Install Chrome, Chromium, Brave, Edge, Arc,")
        print("or Vivaldi; Firefox will NOT work (the extension is MV3).")
        print()
    else:
        browsers = ", ".join(env.browser_info.chromium_found)
        print(f"Detected Chromium-based browser(s): {browsers}")
        print()
    print("1. Open chrome://extensions/ in your Chromium-based browser.")
    print("2. Toggle 'Developer mode' on (top right).")
    print("3. Click 'Load unpacked' and select this directory:")
    print(f"     {ext_dir}")
    print()


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def write_manifest(env: Env, service_type: str, runner: Runner) -> None:
    data = {
        "repo_path": str(env.repo),
        "venv_python": str(env.venv_python),
        "requirements_sha256": _sha256(env.requirements),
        "service_type": service_type,
        "port": env.port,
        "schema_version": SCHEMA_VERSION,
        "installed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    runner.write(env.manifest_path, json.dumps(data, indent=2) + "\n", mode=0o644)


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def print_summary(env: Env, service_type: str, claude_status: str) -> None:
    print()
    print("==============================================================")
    print("  twitter-memory install complete")
    print("==============================================================")
    print(f"  OS                   : {env.os_info.kind}" +
          (f" ({env.os_info.distro})" if env.os_info.distro else ""))
    print(f"  Python               : {env.python_info.version_str} ({env.python_info.executable})")
    print(f"  Data dir             : {env.data_dir}")
    print(f"  Backend port         : {env.port}")
    print(f"  Service              : {service_type}")
    print(f"  Claude Desktop MCP   : {claude_status}")
    print(f"  Extension source     : {env.repo / 'extension'}")
    print()
    print("  Restart Claude Desktop to pick up the MCP config.")
    print("  Then ask Claude: 'export my Twitter day for today'.")
    print()
    print("  Manage the backend:")
    if service_type == "systemd":
        print("    systemctl --user status twitter-memory")
        print("    systemctl --user stop   twitter-memory")
        print("    journalctl --user -u twitter-memory -f")
    elif service_type == "launchd":
        print("    launchctl list | grep twitter-memory")
        print(f"    tail -f ~/Library/Logs/twitter-memory/stderr.log")
    print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="One-click install for twitter-memory")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--skip-claude", action="store_true")
    p.add_argument("--skip-extension", action="store_true")
    p.add_argument("--autostart-only", action="store_true",
                   help="Only (re)install the autostart service.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite a divergent Claude Desktop MCP entry if one exists.")
    p.add_argument("--yes", action="store_true",
                   help="Non-interactive: assume defaults for all prompts.")
    args = p.parse_args(argv)
    args.interactive = not args.yes
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = Runner(dry_run=args.dry_run)

    env = preflight(args)

    if args.autostart_only:
        ensure_data_dir(env, runner)
        service_type = install_service(env, runner, args)
        if service_type != "none":
            wait_for_health(env, runner)
        write_manifest(env, service_type, runner)
        print(f"\nAutostart service re-installed: {service_type}")
        return 0

    ensure_data_dir(env, runner)
    service_type = install_service(env, runner, args)

    claude_status = "skipped"
    if not args.skip_claude:
        claude_status = merge_claude(env, runner, args)

    health_ok = True
    if service_type != "none":
        health_ok = wait_for_health(env, runner)

    if not args.skip_extension:
        extension_nudge(env)

    write_manifest(env, service_type, runner)

    print_summary(env, service_type, claude_status)

    return 0 if health_ok else 1


def uninstall(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Uninstall twitter-memory service + Claude config entry.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--clean", action="store_true", help="Also remove .venv")
    p.add_argument("--purge", action="store_true",
                   help="Remove ~/.twitter-memory entirely. Requires typing DELETE.")
    p.add_argument("--yes", action="store_true",
                   help="Non-interactive: skip prompts (not honored for --purge).")
    args = p.parse_args(argv)
    runner = Runner(dry_run=args.dry_run)

    repo = Path(__file__).resolve().parents[2]
    venv_python = repo / ".venv" / "bin" / "python"
    os_info = detect.detect_os()

    # Service
    if os_info.kind == "linux":
        cfg = service_linux.LinuxServiceConfig(
            repo=repo,
            venv_python=venv_python,
            env_file=detect.home() / ".twitter-memory" / "env",
            unit_dir=detect.home() / ".config" / "systemd" / "user",
            template=repo / "scripts" / "systemd" / "twitter-memory.service.template",
        )
        service_linux.uninstall(cfg, runner)
    else:
        cfg = service_macos.MacServiceConfig(
            repo=repo,
            venv_python=venv_python,
            env_file=detect.home() / ".twitter-memory" / "env",
            plist_dir=detect.home() / "Library" / "LaunchAgents",
            logs_dir=detect.home() / "Library" / "Logs" / "twitter-memory",
        )
        service_macos.uninstall(cfg, runner)

    # Claude config
    if not args.dry_run:
        subprocess.run(
            [str(venv_python), "-m", "scripts.install_helpers.merge_claude_config", "--remove"],
            cwd=str(repo), check=False,
        )
    else:
        print(f"[dry-run] would remove twitter-memory from Claude Desktop MCP config")

    # Manifest
    manifest_path = detect.home() / ".twitter-memory" / ".install-manifest.json"
    runner.remove(manifest_path)

    # Optional cleanup
    if args.clean:
        runner.remove(repo / ".venv")

    if args.purge:
        data_dir = detect.home() / ".twitter-memory"
        if args.dry_run:
            print(f"[dry-run] would remove {data_dir} (requires typing DELETE)")
        else:
            print(f"\n*** You asked to --purge {data_dir}.")
            print("*** This deletes your SQLite database, exports, and all backups.")
            try:
                confirm = input("Type DELETE to confirm: ").strip()
            except EOFError:
                confirm = ""
            if confirm == "DELETE":
                runner.remove(data_dir)
            else:
                print("Aborted --purge. Data preserved.")
                return 2

    print("\nUninstall complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

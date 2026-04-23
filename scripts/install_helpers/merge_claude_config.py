"""Atomic JSON merge for Claude Desktop's ``claude_desktop_config.json``.

Standalone-callable so the installer can shell out and so tests can exercise
the real file I/O with a pytest ``tmp_path``::

    python -m scripts.install_helpers.merge_claude_config \\
        --add --venv-python /abs/.venv/bin/python --repo /abs

    python -m scripts.install_helpers.merge_claude_config --remove

    python -m scripts.install_helpers.merge_claude_config --check \\
        --venv-python /abs/.venv/bin/python --repo /abs

Exit codes:
    0 — success / no-op
    1 — conflict (existing entry differs); rerun with --force
    2 — refuse (malformed JSON file)
    3 — absent (--check only, entry missing)
    4 — usage error
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENTRY_NAME = "twitter-memory"


# -- path resolution --------------------------------------------------------

def default_config_path() -> Path:
    """Resolve Claude Desktop's config path per OS."""
    home = Path(os.path.expanduser("~"))
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Linux":
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    raise RuntimeError(f"Unsupported OS for Claude Desktop config: {system}")


# -- shape of our MCP entry -------------------------------------------------

def desired_entry(venv_python: str, repo: str) -> dict:
    """The single source of truth for the stanza we install into
    ``mcpServers.twitter-memory``."""
    return {
        "command": venv_python,
        "args": ["-m", "mcp_server.server"],
        "env": {},
        "cwd": repo,
    }


# -- atomic I/O -------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load JSON or raise a typed error. Callers differentiate absent (create
    default) vs malformed (refuse) vs valid."""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


def _timestamped_backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(path.name + f".bak-{stamp}")


def _atomic_write(path: Path, content: str) -> None:
    """Write atomically via same-directory temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# -- operations -------------------------------------------------------------

@dataclass
class Result:
    status: str             # "created" | "unchanged" | "updated" | "conflict" | "malformed" | "absent" | "removed"
    backup: Path | None = None
    message: str = ""

    @property
    def exit_code(self) -> int:
        return {
            "created": 0,
            "unchanged": 0,
            "updated": 0,
            "removed": 0,
            "conflict": 1,
            "malformed": 2,
            "absent": 3,
        }.get(self.status, 4)


def add(
    config_path: Path,
    entry: dict,
    *,
    force: bool = False,
    yes: bool = False,
) -> Result:
    """Install the twitter-memory MCP entry. Backs up + atomically writes."""
    # 1. Load (or treat as empty if file doesn't exist yet)
    if not config_path.exists():
        data: dict = {}
    else:
        try:
            data = _load_json(config_path)
        except json.JSONDecodeError as e:
            return Result(
                status="malformed",
                message=(
                    f"Claude Desktop config at {config_path} is not valid JSON "
                    f"(line {e.lineno}, col {e.colno}: {e.msg}). Installer will "
                    "not touch an unparseable config — fix the file manually, "
                    "then re-run."
                ),
            )

    if not isinstance(data, dict):
        return Result(
            status="malformed",
            message=f"Config at {config_path} is valid JSON but not an object.",
        )

    mcp_servers = data.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        return Result(
            status="malformed",
            message=f"mcpServers in {config_path} is not an object.",
        )

    existing = mcp_servers.get(ENTRY_NAME)
    if existing == entry:
        return Result(status="unchanged", message="Entry already present and matches.")

    if existing and not force and not yes:
        return Result(
            status="conflict",
            message=(
                f"An existing {ENTRY_NAME} entry in {config_path} differs "
                "from what install.sh would write. Re-run with --force to "
                f"overwrite (or edit the file by hand).\n\n"
                f"Current: {json.dumps(existing, indent=2)}\n"
                f"New:     {json.dumps(entry, indent=2)}"
            ),
        )

    # 2. Backup if the file existed with prior content.
    backup: Path | None = None
    if config_path.exists():
        backup = _timestamped_backup(config_path)
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. Apply + write.
    mcp_servers[ENTRY_NAME] = entry
    data["mcpServers"] = mcp_servers
    _atomic_write(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    status = "updated" if existing else ("created" if not config_path.exists() or backup else "created")
    # (above: if file didn't exist before, we've now created it — status is "created")
    if not backup:
        status = "created"
    return Result(status=status, backup=backup, message=f"Wrote {config_path}")


def remove(config_path: Path) -> Result:
    if not config_path.exists():
        return Result(status="absent", message=f"No config at {config_path}.")

    try:
        data = _load_json(config_path)
    except json.JSONDecodeError as e:
        return Result(
            status="malformed",
            message=(
                f"Claude Desktop config at {config_path} is not valid JSON "
                f"(line {e.lineno}, col {e.colno}: {e.msg}). Refusing to edit."
            ),
        )

    if not isinstance(data, dict):
        return Result(status="malformed", message="Config is not a JSON object.")

    mcp = data.get("mcpServers")
    if not isinstance(mcp, dict) or ENTRY_NAME not in mcp:
        return Result(status="absent", message=f"No {ENTRY_NAME} entry to remove.")

    backup = _timestamped_backup(config_path)
    backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    mcp.pop(ENTRY_NAME, None)
    _atomic_write(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return Result(status="removed", backup=backup, message=f"Removed {ENTRY_NAME} from {config_path}")


def check(config_path: Path, entry: dict) -> Result:
    if not config_path.exists():
        return Result(status="absent", message="Config file missing.")
    try:
        data = _load_json(config_path)
    except json.JSONDecodeError as e:
        return Result(status="malformed", message=f"Malformed JSON: {e}")
    if not isinstance(data, dict):
        return Result(status="malformed", message="Not a JSON object.")
    existing = (data.get("mcpServers") or {}).get(ENTRY_NAME)
    if existing is None:
        return Result(status="absent")
    if existing == entry:
        return Result(status="unchanged")
    return Result(status="conflict", message="Entry present but differs.")


# -- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--add", action="store_true")
    action.add_argument("--remove", action="store_true")
    action.add_argument("--check", action="store_true")
    p.add_argument("--venv-python", help="Absolute path to .venv/bin/python")
    p.add_argument("--repo", help="Absolute path to the repo (cwd for the MCP stdio server)")
    p.add_argument("--config-path", help="Override Claude Desktop config path (for tests)")
    p.add_argument("--force", action="store_true", help="Overwrite divergent existing entry")
    p.add_argument("--yes", action="store_true", help="Assume yes for prompts (non-interactive)")
    args = p.parse_args(argv)

    config_path = Path(args.config_path).expanduser() if args.config_path else default_config_path()

    if args.remove:
        res = remove(config_path)
    elif args.check:
        if not args.venv_python or not args.repo:
            p.error("--check requires --venv-python and --repo")
        res = check(config_path, desired_entry(args.venv_python, args.repo))
    else:  # --add
        if not args.venv_python or not args.repo:
            p.error("--add requires --venv-python and --repo")
        res = add(
            config_path,
            desired_entry(args.venv_python, args.repo),
            force=args.force,
            yes=args.yes,
        )

    if res.message:
        print(res.message, file=sys.stderr if res.exit_code else sys.stdout)
    if res.backup:
        print(f"(backup: {res.backup})")
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

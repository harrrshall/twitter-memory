"""Unit tests for the pure-logic pieces of the installer.

The installer itself drives systemd / launchctl / Claude Desktop side
effects and isn't meaningfully testable without broad mocking. What IS
testable — and highest-risk — is:

- ``merge_claude_config``: every transition of the JSON merge state machine
- ``detect.find_free_port``: port probing under an actual port bind
- ``runner.Runner``: dry-run mode produces no writes
- ``service_linux.render_unit`` / ``service_macos.render_plist``: template
  → string is pure; golden-value assertions.
"""
from __future__ import annotations

import json
import os
import plistlib
import socket
from pathlib import Path

import pytest

from scripts.install_helpers import detect, merge_claude_config, runner as runner_mod
from scripts.install_helpers import service_linux, service_macos


# --------------------------------------------------------------------------
# merge_claude_config
# --------------------------------------------------------------------------

ENTRY = merge_claude_config.desired_entry(
    venv_python="/abs/.venv/bin/python",
    repo="/abs/repo",
)


def test_add_creates_config_when_absent(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    res = merge_claude_config.add(cfg, ENTRY)
    assert res.status == "created"
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["twitter-memory"] == ENTRY


def test_add_preserves_other_mcp_servers(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "/usr/bin/other"}}
    }))
    res = merge_claude_config.add(cfg, ENTRY)
    assert res.status == "created" or res.status == "updated"
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["other"] == {"command": "/usr/bin/other"}
    assert body["mcpServers"]["twitter-memory"] == ENTRY


def test_add_is_noop_when_entry_matches(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"twitter-memory": ENTRY}}))
    res = merge_claude_config.add(cfg, ENTRY)
    assert res.status == "unchanged"
    assert res.backup is None


def test_add_conflicts_when_entry_differs_without_force(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    old = {"command": "/different/python", "args": ["-m", "mcp_server.server"], "env": {}, "cwd": "/old"}
    cfg.write_text(json.dumps({"mcpServers": {"twitter-memory": old}}))
    res = merge_claude_config.add(cfg, ENTRY, force=False, yes=False)
    assert res.status == "conflict"
    # File is untouched
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["twitter-memory"] == old


def test_add_overwrites_with_force(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    old = {"command": "/different/python", "args": [], "env": {}}
    cfg.write_text(json.dumps({"mcpServers": {"twitter-memory": old}}))
    res = merge_claude_config.add(cfg, ENTRY, force=True)
    assert res.status == "updated"
    assert res.backup is not None and res.backup.is_file()
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["twitter-memory"] == ENTRY
    # Backup preserves the pre-overwrite state
    assert json.loads(res.backup.read_text())["mcpServers"]["twitter-memory"] == old


def test_add_refuses_on_malformed_json(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{ "mcpServers": { "broken": ')  # deliberately truncated
    res = merge_claude_config.add(cfg, ENTRY)
    assert res.status == "malformed"
    assert res.exit_code == 2
    # Crucially: untouched
    assert cfg.read_text() == '{ "mcpServers": { "broken": '


def test_add_backs_up_before_write(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"other": {"command": "/usr/bin/other"}}}
    cfg.write_text(json.dumps(original))
    res = merge_claude_config.add(cfg, ENTRY)
    # Backup exists and equals the pre-write content
    assert res.backup is not None
    assert json.loads(res.backup.read_text()) == original


def test_remove_drops_our_entry_only(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "twitter-memory": ENTRY,
            "other": {"command": "/usr/bin/other"},
        }
    }))
    res = merge_claude_config.remove(cfg)
    assert res.status == "removed"
    body = json.loads(cfg.read_text())
    assert "twitter-memory" not in body["mcpServers"]
    assert "other" in body["mcpServers"]


def test_remove_is_absent_on_missing_entry(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "/usr/bin/other"}}}))
    res = merge_claude_config.remove(cfg)
    assert res.status == "absent"


def test_remove_absent_when_config_missing(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    res = merge_claude_config.remove(cfg)
    assert res.status == "absent"


def test_check_matches(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"twitter-memory": ENTRY}}))
    assert merge_claude_config.check(cfg, ENTRY).status == "unchanged"


def test_check_detects_conflict(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    stale = dict(ENTRY, cwd="/stale")
    cfg.write_text(json.dumps({"mcpServers": {"twitter-memory": stale}}))
    assert merge_claude_config.check(cfg, ENTRY).status == "conflict"


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    merge_claude_config.add(cfg, ENTRY)
    # No .tmp files from NamedTemporaryFile
    tmps = [p for p in tmp_path.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name]
    assert tmps == []


# --------------------------------------------------------------------------
# detect.find_free_port
# --------------------------------------------------------------------------

def test_find_free_port_returns_preferred_when_free():
    # Pick a high port that's very unlikely to be in use.
    assert detect.find_free_port(preferred=54321, fallback_range=(54322, 54330)) == 54321


def test_find_free_port_probes_fallback_when_preferred_busy():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 54340))
    s.listen(1)
    try:
        port = detect.find_free_port(preferred=54340, fallback_range=(54341, 54345))
        assert 54341 <= port <= 54345
    finally:
        s.close()


def test_port_in_use_true_when_bound():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 54350))
    s.listen(1)
    try:
        assert detect.port_in_use(54350) is True
    finally:
        s.close()


# --------------------------------------------------------------------------
# detect.parse_env_file / render_env_file
# --------------------------------------------------------------------------

def test_env_file_round_trip(tmp_path: Path):
    path = tmp_path / "env"
    values = {"TWITTER_MEMORY_DATA": "/abs/data", "TWITTER_MEMORY_PORT": "8765"}
    path.write_text(detect.render_env_file(values))
    parsed = detect.parse_env_file(path)
    assert parsed == values


def test_env_file_ignores_comments_and_blanks(tmp_path: Path):
    path = tmp_path / "env"
    path.write_text("# comment\n\nKEY=VAL\n # with spaces\nOTHER=x\n")
    parsed = detect.parse_env_file(path)
    assert parsed == {"KEY": "VAL", "OTHER": "x"}


# --------------------------------------------------------------------------
# runner.Runner
# --------------------------------------------------------------------------

def test_runner_write_touches_disk_in_real_mode(tmp_path: Path):
    r = runner_mod.Runner(dry_run=False)
    p = tmp_path / "a.txt"
    r.write(p, "hello")
    assert p.read_text() == "hello"


def test_runner_write_is_noop_in_dry_run(tmp_path: Path):
    r = runner_mod.Runner(dry_run=True)
    p = tmp_path / "a.txt"
    r.write(p, "hello")
    assert not p.exists()
    assert any("write" in a for a in r.actions)


def test_runner_mkdir_idempotent(tmp_path: Path):
    r = runner_mod.Runner(dry_run=False)
    r.mkdir(tmp_path / "nested" / "dir")
    r.mkdir(tmp_path / "nested" / "dir")  # re-run
    assert (tmp_path / "nested" / "dir").is_dir()


def test_runner_write_unchanged_when_content_matches(tmp_path: Path):
    r = runner_mod.Runner(dry_run=False)
    p = tmp_path / "a.txt"
    p.write_text("same")
    r.write(p, "same")
    assert any("unchanged" in a for a in r.actions)


# --------------------------------------------------------------------------
# service_linux.render_unit
# --------------------------------------------------------------------------

def test_render_unit_substitutes_all_placeholders(tmp_path: Path):
    template = tmp_path / "t.service.template"
    template.write_text(
        "WorkingDirectory={{REPO}}\n"
        "EnvironmentFile={{ENV_FILE}}\n"
        "ExecStart={{VENV_PYTHON}} -m backend.main\n"
    )
    cfg = service_linux.LinuxServiceConfig(
        repo=Path("/abs/repo"),
        venv_python=Path("/abs/.venv/bin/python"),
        env_file=Path("/home/u/.twitter-memory/env"),
        unit_dir=Path("/home/u/.config/systemd/user"),
        template=template,
    )
    out = service_linux.render_unit(cfg)
    assert "WorkingDirectory=/abs/repo" in out
    assert "EnvironmentFile=/home/u/.twitter-memory/env" in out
    assert "ExecStart=/abs/.venv/bin/python -m backend.main" in out
    # No unresolved placeholders
    assert "{{" not in out


def test_render_unit_against_shipped_template():
    """Sanity check that the template file we actually ship renders cleanly."""
    repo = Path(__file__).resolve().parents[1]
    template = repo / "scripts" / "systemd" / "twitter-memory.service.template"
    assert template.is_file(), "shipped template missing"
    cfg = service_linux.LinuxServiceConfig(
        repo=Path("/abs/repo"),
        venv_python=Path("/abs/.venv/bin/python"),
        env_file=Path("/home/u/.twitter-memory/env"),
        unit_dir=Path("/home/u/.config/systemd/user"),
        template=template,
    )
    out = service_linux.render_unit(cfg)
    assert "{{" not in out
    assert "twitter-memory local ingest backend" in out
    assert "ExecStart=/abs/.venv/bin/python -m backend.main" in out


# --------------------------------------------------------------------------
# service_macos.render_plist
# --------------------------------------------------------------------------

def test_render_plist_parses_back_to_expected_dict(tmp_path: Path):
    env_file = tmp_path / "env"
    env_file.write_text("TWITTER_MEMORY_PORT=8765\nTWITTER_MEMORY_DATA=/abs/data\n")
    cfg = service_macos.MacServiceConfig(
        repo=Path("/abs/repo"),
        venv_python=Path("/abs/.venv/bin/python"),
        env_file=env_file,
        plist_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "Logs",
    )
    raw = service_macos.render_plist(cfg)
    data = plistlib.loads(raw)
    assert data["Label"] == "com.local.twitter-memory"
    assert data["ProgramArguments"] == ["/abs/.venv/bin/python", "-m", "backend.main"]
    assert data["WorkingDirectory"] == "/abs/repo"
    assert data["EnvironmentVariables"]["TWITTER_MEMORY_PORT"] == "8765"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert str(tmp_path / "Logs" / "stderr.log") == data["StandardErrorPath"]


def test_render_plist_empty_env_file(tmp_path: Path):
    cfg = service_macos.MacServiceConfig(
        repo=Path("/abs/repo"),
        venv_python=Path("/abs/.venv/bin/python"),
        env_file=tmp_path / "missing-env",
        plist_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "Logs",
    )
    data = plistlib.loads(service_macos.render_plist(cfg))
    assert data["EnvironmentVariables"] == {}

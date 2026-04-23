"""Dry-run-aware side-effect wrapper.

Every filesystem mutation and subprocess invocation in the installer goes
through ``Runner`` so ``--dry-run`` can preview the install without
touching disk or running systemd.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class Runner:
    dry_run: bool = False
    # Captures for tests — what we would have done, in order.
    actions: list[str] = field(default_factory=list)

    def _log(self, line: str) -> None:
        self.actions.append(line)
        prefix = "[dry-run] " if self.dry_run else ""
        print(prefix + line)

    # -- subprocess -----------------------------------------------------------

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        env: dict | None = None,
    ) -> subprocess.CompletedProcess | None:
        """Run a command. In dry-run mode, only prints the command."""
        self._log("run: " + " ".join(cmd))
        if self.dry_run:
            return None
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture_output,
            text=True,
            env=env,
        )

    # -- filesystem -----------------------------------------------------------

    def write(self, path: Path, content: str, *, mode: int = 0o644) -> None:
        """Write a file. In dry-run mode, prints a unified diff against the
        existing content (if any) so the reviewer can audit changes."""
        path = Path(path)
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing == content:
            self._log(f"write (unchanged): {path}")
            return
        self._log(f"write: {path}")
        if self.dry_run:
            diff = difflib.unified_diff(
                existing.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=str(path) + " (before)",
                tofile=str(path) + " (after)",
                n=2,
            )
            preview = "".join(diff) or "  (empty → new file)\n"
            print("\n".join("    " + ln for ln in preview.splitlines()))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.chmod(mode)
        tmp.replace(path)

    def mkdir(self, path: Path, *, mode: int = 0o755) -> None:
        path = Path(path)
        if path.is_dir():
            self._log(f"mkdir (exists): {path}")
            return
        self._log(f"mkdir: {path}")
        if not self.dry_run:
            path.mkdir(parents=True, exist_ok=True, mode=mode)

    def remove(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            self._log(f"remove (absent): {path}")
            return
        self._log(f"remove: {path}")
        if self.dry_run:
            return
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    # -- introspection --------------------------------------------------------

    def last_actions(self) -> Iterable[str]:
        return list(self.actions)

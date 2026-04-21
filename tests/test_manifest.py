"""Static checks on extension/manifest.json.

Regression: /qa 2026-04-21. The manifest listed icons/icon16.png etc. that
didn't exist in the repo, so Chrome refused to load the manifest at all and
none of the content scripts ran. Catching that at test time means the
extension can never ship with dangling file references.
"""
import json
from pathlib import Path


EXT_DIR = Path(__file__).resolve().parent.parent / "extension"


def test_manifest_file_references_all_exist():
    manifest = json.loads((EXT_DIR / "manifest.json").read_text())

    missing: list[str] = []

    def check(rel: str) -> None:
        if not (EXT_DIR / rel).exists():
            missing.append(rel)

    bg = manifest.get("background") or {}
    if bg.get("service_worker"):
        check(bg["service_worker"])

    for cs in manifest.get("content_scripts", []) or []:
        for js in cs.get("js", []) or []:
            check(js)
        for css in cs.get("css", []) or []:
            check(css)

    action = manifest.get("action") or {}
    if action.get("default_popup"):
        check(action["default_popup"])
    for size, path in (action.get("default_icon") or {}).items():
        check(path)

    for size, path in (manifest.get("icons") or {}).items():
        check(path)

    for path in manifest.get("web_accessible_resources", []) or []:
        if isinstance(path, str):
            check(path)

    assert not missing, f"manifest.json references files that don't exist in extension/: {missing}"

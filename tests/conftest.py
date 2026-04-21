import os
import shutil
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch):
    """Isolate TWITTER_MEMORY_DATA per test. Must be set before any backend import
    that reads backend.settings, so we also reimport modules that cached paths."""
    monkeypatch.setenv("TWITTER_MEMORY_DATA", str(tmp_path))
    # Force settings modules to re-read the env var.
    import importlib
    import backend.settings
    import backend.db
    import mcp_server.settings
    importlib.reload(backend.settings)
    importlib.reload(backend.db)
    importlib.reload(mcp_server.settings)
    yield tmp_path

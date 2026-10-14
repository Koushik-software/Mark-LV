"""Shared test fixtures.

The repository root is put on sys.path so the tests can import the app's own
packages (`memory`, `core`, `actions`, `plugins`) exactly as `main.py` does —
no installation step, no packaging shim.

Nothing here imports PyQt6, `google.genai`, `sounddevice` or any other heavy or
platform-specific dependency: the modules under test are deliberately the ones
that import cleanly on a bare interpreter, so this suite runs anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    """Point memory_manager at a throwaway long_term.json so tests never touch
    the real store.

    Also redirects the SQLite/FTS5 index (memory_store) at a throwaway DB and
    clears its cached connection — otherwise save_memory() would sync the test's
    data into the developer's real jarvis_memory.db, and search_memory() would
    read it back."""
    from memory import memory_manager as mm
    from memory import memory_store as ms

    target = tmp_path / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", target)
    monkeypatch.setattr(mm, "_trim_notifier", None)

    ms.reset_for_tests()
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "jarvis_memory.db")
    yield target
    ms.reset_for_tests()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config_manager at a throwaway config dir / api_keys.json."""
    from memory import config_manager as cm

    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(cm, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg_dir / "api_keys.json")
    return cfg_dir / "api_keys.json"

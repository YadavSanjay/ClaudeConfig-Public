"""Shared test setup: review-history writes go to a per-test temp folder, never the real one."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "review-history"
    monkeypatch.setenv("REVIEW_HISTORY_DIR", str(root))
    return root

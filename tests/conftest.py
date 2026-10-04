"""Tests must never read or write the developer's real state (library, helper token) or reach a real helper."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _never_use_real_state(monkeypatch):
    """render_report() looks for state/library.json and state/library.token under the project root. Once `schedule
    install` has run on a developer's Mac those files exist, and a UI test would POST its clicks to the real helper.
    Tests that use a temporary project root (dataclasses.replace(CFG, root=tmp)) are not affected."""
    import dailyread.render as render
    real_helper, real_path = render.helper_config, render.library_path
    monkeypatch.setattr(render, "helper_config", lambda cfg: None if cfg.root == ROOT else real_helper(cfg))
    monkeypatch.setattr(render, "library_path",
                        lambda cfg: ROOT / "state" / "no-such-test-library.json" if cfg.root == ROOT else real_path(cfg))

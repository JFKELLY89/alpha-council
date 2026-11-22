"""
Alpha Council - F14 startup watchdog tests.

A launch that never reaches SESSION_STARTED must die on its own, because
a zombie satisfies the launcher's duplicate guard and blocks every later
self-heal (observed 2026-11-17: four days).

Run:
    uv run pytest tests/test_startup_watchdog.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_alpha_council import arm_startup_watchdog  # noqa: E402


def test_watchdog_fires_when_startup_never_completes():
    fired: list[int] = []
    timer = arm_startup_watchdog(minutes=0.05 / 60.0, _exit=fired.append)
    try:
        deadline = time.monotonic() + 2.0
        while not fired and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fired == [1]
    finally:
        timer.cancel()


def test_watchdog_cancel_disarms_it():
    fired: list[int] = []
    timer = arm_startup_watchdog(minutes=0.05 / 60.0, _exit=fired.append)
    timer.cancel()
    time.sleep(0.2)
    assert fired == []

#!/usr/bin/env python3
"""Compatibility entry point for the isolated natural-conversation benchmark."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.run_benchmark import run_isolated


if __name__ == "__main__":
    raise SystemExit(run_isolated())

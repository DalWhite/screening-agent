"""Paths every test layer needs, independent of where the test file sits."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # tests/support/paths.py -> the repository
BACKEND = REPO / "backend"

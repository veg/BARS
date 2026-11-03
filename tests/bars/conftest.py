"""Make the in-tool `bars` package importable and expose shared fixture paths."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO / "galaxy" / "bars_review"
TEST_DATA = TOOL_DIR / "test-data"
sys.path.insert(0, str(TOOL_DIR))


@pytest.fixture
def test_data() -> Path:
    return TEST_DATA

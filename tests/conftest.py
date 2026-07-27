"""Shared fixtures for the test suite."""

from collections import Counter
from pathlib import Path

import pytest

from app.etl.contract import infer_cycler
from app.etl.pipeline import discover_files

REPO_DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session")
def repo_cycler_counts() -> dict[str, int]:
    """Number of source files per cycler under data/.

    Computed from the dataset itself rather than hardcoded, so the suite
    doesn't need updating every time a file is added to or removed from data/.
    """
    cyclers = (infer_cycler(path) for path in discover_files(REPO_DATA))
    return dict(Counter(cycler for cycler in cyclers if cycler != "unknown"))


@pytest.fixture(scope="session")
def repo_test_count(repo_cycler_counts) -> int:
    """Total number of source files under data/ that should become tests."""
    return sum(repo_cycler_counts.values())

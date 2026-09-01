from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def root_dir() -> str:
    """Repo root, derived from this file - portable across machines."""
    assert (REPO_ROOT / "data").is_dir(), "expected data/ next to tests/"
    return str(REPO_ROOT)


@pytest.fixture(scope="session")
def store(root_dir):
    """Read-only across the suite, so build the in-memory DB once."""
    from harbourview_agent.data_store import DataStore

    return DataStore(root_dir)


@pytest.fixture()
def router(root_dir):
    """Deterministic router - no API key, no network."""
    from harbourview_agent.agent import HarbourviewAgent

    return HarbourviewAgent(root_dir, use_llm=False)

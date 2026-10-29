import copy
import socket
from pathlib import Path

import pytest

from pipeline.athlete import Athlete, load_athlete
from pipeline.common import load_yaml

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "config" / "plan.yaml"
ATHLETE = ROOT / "tests" / "fixtures" / "athlete.yaml"  # frozen anchors (bike FTP 300 W): see the file
REAL_ATHLETE = ROOT / "config" / "athlete.yaml"


def use_test_athlete(config_dir: Path) -> None:
    """Put the frozen test anchors into a copied config/ directory."""
    (config_dir / "athlete.yaml").write_bytes(ATHLETE.read_bytes())

_PLAN_DATA = load_yaml(PLAN)
_ATHLETE_DATA = load_yaml(ATHLETE)


@pytest.fixture(autouse=True)
def no_live_changes(monkeypatch):
    """The real training/logs/changes.yaml never reaches a test (subprocesses inherit this too);
    tests that want changes pass changes_path or set PEDESTAL_CHANGES themselves."""
    monkeypatch.setenv("PEDESTAL_CHANGES", "none")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the internet; HTTP goes through httpx.MockTransport."""

    def refuse(*args, **kwargs):
        raise RuntimeError("network access in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def plan_data() -> dict:
    """A fresh copy of config/plan.yaml as parsed data, safe to mutate."""
    return copy.deepcopy(_PLAN_DATA)


@pytest.fixture
def athlete_data() -> dict:
    return copy.deepcopy(_ATHLETE_DATA)


@pytest.fixture(scope="session")
def athlete() -> Athlete:
    return load_athlete(ATHLETE)

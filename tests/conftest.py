import copy
from pathlib import Path

import pytest

from pipeline.athlete import Athlete, load_athlete
from pipeline.common import load_yaml

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "config" / "plan.yaml"
ATHLETE = ROOT / "config" / "athlete.yaml"

_PLAN_DATA = load_yaml(PLAN)
_ATHLETE_DATA = load_yaml(ATHLETE)


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

"""Secrets and other settings, from environment variables only.

`load_env()` reads the local `.env` (git-ignored) into the environment without
overriding anything already set. `require_env()` fetches one variable and fails
with a message naming the variable, never its value.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


class MissingSetting(RuntimeError):
    pass


def load_env(path: str | Path | None = None) -> None:
    """Load `.env` (default: the nearest one above the working directory) into os.environ."""
    load_dotenv(path or find_dotenv(usecwd=True), override=False)


def require_env(name: str, env: Mapping[str, str] | None = None) -> str:
    value = (os.environ if env is None else env).get(name, "").strip()
    if not value:
        raise MissingSetting(f"{name} is not set. Add it to .env (see .env.example).")
    return value

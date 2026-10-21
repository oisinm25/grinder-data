"""The .githooks/pre-commit secret check, run by git in a throwaway repo.

Fake credentials are assembled at runtime so this file never contains one itself.
"""

import shutil
import subprocess

import pytest

from tests.conftest import ROOT

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

HOOKS = (ROOT / ".githooks").as_posix()


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "core.hooksPath", HOOKS)
    return tmp_path


def commit(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", "-f", name)
    return git(repo, "commit", "-q", "-m", "test")


def test_clean_commit_passes(repo):
    r = commit(repo, "pipeline/x.py", 'api_key = os.environ["INTERVALS_ICU_API_KEY"]\n')
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "text",
    [
        "token = '" + "github" + "_pat_" + "A1b2C3d4" * 6 + "'\n",
        "GH = " + "gh" + "p_" + "x" * 36 + "\n",
        "client(key='" + "sk-" + "ant-" + "q" * 40 + "')\n",
        'API_KEY = "' + "a1B2c3D4" * 3 + '"\n',
        "INTERVALS_ICU_API_KEY=" + "9f8e7d6c" * 3 + "\n",
    ],
    ids=["github_pat", "ghp", "sk-ant", "quoted-literal", "env-style"],
)
def test_secret_is_blocked_without_echoing_it(repo, text):
    r = commit(repo, "pipeline/x.py", text)
    assert r.returncode != 0
    assert "pipeline/x.py" in r.stderr
    secret = text.split("=", 1)[1].strip(" \n'\")(")
    assert secret[-12:] not in r.stderr + r.stdout


@pytest.mark.parametrize("name", [".env", ".venv/lib.py", "derived/prescriptions.csv"])
def test_local_only_paths_are_blocked(repo, name):
    r = commit(repo, name, "x\n")
    assert r.returncode != 0
    assert name in r.stderr


def test_env_example_with_placeholders_passes(repo):
    r = commit(repo, ".env.example", "INTERVALS_ICU_API_KEY=\nINTERVALS_ICU_ATHLETE_ID=<your id>\n")
    assert r.returncode == 0, r.stderr

import datetime as dt
import json

import pytest

from pipeline.gate import decide, main, parse_window, read_state

W = (5, 11)
DAY = dt.date(2026, 10, 26)


def at(h, m=17, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m))


@pytest.mark.parametrize(
    ("hour", "run", "final"),
    [
        (3, False, False),  # before the window
        (4, False, False),
        (5, True, False),  # window opens: wait for sleep data
        (8, True, False),
        (10, True, True),  # last hour: fallback, run regardless
        (11, True, True),  # catch-up, in case GitHub skipped a slot
        (12, True, True),
        (13, False, False),  # given up for the day
        (23, False, False),
    ],
)
def test_window(hour, run, final):
    assert decide(at(hour), W, {})[:2] == (run, final)


def test_once_a_day():
    sent = {"last_done": "2026-10-26", "done_at": "07:14"}
    assert decide(at(8), W, sent) == (False, False, "already ran today (07:14)")
    assert decide(at(8, day=DAY + dt.timedelta(days=1)), W, sent)[0]  # a new day starts fresh


def test_manual_run_always_goes_as_final():
    assert decide(at(22), W, {"last_done": "2026-10-26"}, forced=True)[:2] == (True, True)


def test_parse_window():
    assert parse_window("05-11") == (5, 11) and parse_window("6-10") == (6, 10)
    with pytest.raises(ValueError, match="PEDESTAL_WINDOW"):
        parse_window("11-05")


def test_missing_or_broken_state_reads_empty(tmp_path):
    assert read_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_state(bad) == {}


def test_cli_writes_github_output(tmp_path, monkeypatch, capsys):
    out = tmp_path / "gh_output"
    state = tmp_path / "morning.json"
    state.write_text(json.dumps({"last_done": "2026-10-25"}))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("TZ", "Europe/Dublin")
    assert main(["--state", str(state), "--window", "05-11"], now=at(10)) == 0
    assert out.read_text() == "run=true\nfinal=true\n"
    assert "2026-10-26 10:17 Europe/Dublin: run (final) - last chance" in capsys.readouterr().out


def test_cli_window_from_env(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("PEDESTAL_WINDOW", "07-09")
    main(["--state", str(tmp_path / "none.json")], now=at(6))
    assert out.read_text() == "run=false\nfinal=false\n"

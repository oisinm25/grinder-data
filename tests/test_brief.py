import csv
import datetime as dt
import json

import pytest

from pipeline.brief import build, main, short_readiness, to_markdown, write
from pipeline.plan.schema import validate_plan
from pipeline.prescribe import LogRow, prescribe, write_prescriptions
from tests.conftest import ROOT

D = dt.date
MON, TUE = D(2026, 10, 26), D(2026, 10, 27)
TEST_BENCH = LogRow(D(2026, 10, 19), "test_bench", "bench_press", "amrap", 100, 8, 8.5)
AMBER = "amber: HRV 30 < 32.9 (baseline 44.1 - 1 SD 11.2); resting HR 61 <= 62.4 (baseline 57.4 + 5); sleep 7.4 h >= 6"


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def derived(tmp_path, plan, athlete):
    d = tmp_path / "derived"
    d.mkdir()
    write_rows(d / "wellness.csv", [{"date": "2026-10-26", "readiness": "amber", "readiness_reason": AMBER}])
    write_prescriptions(d / "prescriptions.csv", prescribe(plan, athlete, [TEST_BENCH], {}, MON, D(2026, 11, 1)), MON, D(2026, 11, 1))
    write_rows(d / "compliance.csv", [
        {"date": "2026-10-26", "slot": "mon_pm", "session": "z2_90", "modality": "bike", "item": "session", "planned": "", "actual": "", "grade": "held", "note": "2/2 steps in band"},
        {"date": "2026-10-26", "slot": "mon_am", "session": "upper_me", "modality": "strength", "item": "bench_press top", "planned": "", "actual": "", "grade": "under", "note": "reps under 6"},
    ])  # fmt: skip
    write_rows(d / "weekly.csv", [{"week_end": "2026-10-25", "flags": "sessions: 3 endurance sessions missed or skipped | recovery: 2 red days"}])
    return d


def test_short_readiness():
    assert short_readiness(AMBER) == "HRV 30 < 32.9; RHR 61 <= 62.4; sleep 7.4 h >= 6"


def test_structured_brief(plan, athlete, derived):
    b = build(plan, athlete, MON, derived)
    assert b["date"] == "2026-10-26"
    assert b["readiness"] == {"colour": "amber", "reason": AMBER}
    assert b["block"] == {"id": "block1", "name": "Accumulation", "week": "acc1", "design_pending": False}
    upper, z2 = b["sessions"]
    assert (upper["slot"], upper["session"], upper["modality"], upper["tss"]) == ("mon_am", "upper_me", "strength", 50)
    top = upper["strength"][0]
    assert {k: top[k] for k in ("name", "role", "sets", "reps", "kg", "rpe_cap", "basis")} == {
        "name": "Bench press", "role": "top", "sets": 1, "reps": 6, "kg": 105, "rpe_cap": 8, "basis": "e1rm",
    }  # fmt: skip
    assert top["reason"].startswith("e1RM 132 from 100x8 @8.5 on 19 Oct")
    assert upper["strength"][2]["kg"] is None  # a "choose" group
    assert (z2["slot"], z2["duration_min"]) == ("mon_pm", 90)
    assert z2["steps"][1] == {"kind": "steady", "duration_s": 5100, "target": "195-216 W", "anchor": "pct_ftp", "watts": [195, 216], "provisional": False}
    assert b["yesterday"] == {"date": "2026-10-25", "grades": [], "status": "rest day"}
    assert b["review"] == {"week_end": "2026-10-25", "flags": ["sessions: 3 endurance sessions missed or skipped", "recovery: 2 red days"]}
    assert b["pipeline"] == {"failed": [], "skipped": []}


def test_markdown(plan, athlete, derived):
    md = to_markdown(build(plan, athlete, MON, derived, failed=["publish"]))
    assert md.startswith("# Pedestal brief: Mon 26 Oct 2026\n")
    assert "**Readiness: AMBER**: HRV 30 < 32.9 (baseline 44.1 - 1 SD 11.2);" in md
    assert "> Pipeline: publish failed today; this brief may use older data." in md
    assert "## AM: Upper max effort - bench top set, back-offs, heavy pulling\nstrength · 50 TSS" in md
    assert "| Bench press | top | 1 × 6 | 105 | 8 | e1RM 132 from 100x8 @8.5 on 19 Oct -> 105 x 6 @RPE 8; readiness not known yet |" in md
    assert "| Weighted pull-up | work | 4 × 5 | choose | 8.5 |" in md
    assert "## PM: Z2 90 min\nbike · 90 min · 70 TSS\n\n- warmup 5:00 150-186 W\n- steady 1:25:00 195-216 W" in md
    assert "## Yesterday (Sun 25 Oct)\nRest day." in md
    assert "## Flags (review of the week ending Sun 25 Oct)\n- sessions: 3 endurance sessions missed or skipped\n- recovery: 2 red days" in md


def test_watts_repeats_and_yesterdays_grades(plan, athlete, derived):
    b = build(plan, athlete, TUE, derived)
    assert b["readiness"]["colour"] is None and b["readiness"]["reason"] == "no wellness data for today yet"
    hb = b["sessions"][0]
    assert hb["provisional_ftp"] is True and hb["steps"][1]["kind"] == "repeat" and hb["steps"][1]["count"] == 5
    md = to_markdown(b)
    assert "- 5 × [work 4:00 265-277 W*, rest 3:00 113-139 W*]" in md and "\\* watts from a provisional FTP" in md
    assert "- **held** z2_90: 2/2 steps in band" in md and "- **under** upper_me: bench_press top: reps under 6" in md


def test_no_sleep_note_and_special_days(plan, athlete, derived):
    b = build(plan, athlete, TUE, derived, missing_note="no HRV/sleep synced by 10:17")
    assert "**Readiness: no call**: no HRV/sleep synced by 10:17" in to_markdown(b)
    assert "Rest day." in to_markdown(build(plan, athlete, D(2026, 10, 25), derived))
    assert "Design pending: no sessions planned yet." in to_markdown(build(plan, athlete, D(2026, 11, 24), derived))


def test_write_both_files(plan, athlete, derived):
    md, js = write(build(plan, athlete, MON, derived), derived)
    assert md.name == "today.md" and js.name == "today.json"
    assert json.loads(js.read_text(encoding="utf-8"))["sessions"][0]["session"] == "upper_me"


def test_cli(derived, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["--day", "2026-10-26", "--derived", str(derived), "--print"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Pedestal brief: Mon 26 Oct 2026") and "wrote" in out
    assert (derived / "today.md").exists() and (derived / "today.json").exists()

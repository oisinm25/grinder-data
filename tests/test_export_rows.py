import json
import subprocess

import pytest

from pipeline.dashboard import export_rows as ex


def dashboard():
    return {
        "date": "2026-10-27", "status": "final", "generated_at": "2026-10-27T06:40+01:00",
        "today": {"readiness": {"colour": "amber", "reason": "amber: HRV 30 < 32.9"}, "sessions": []},
        "charts": {
            "fitness": {
                "days": [{"date": "2026-10-26", "load": 70, "ctl": 33.0, "atl": 45.0, "form": -12.0, "icu_ctl": 32.0, "icu_atl": 44.0},
                         {"date": "2026-10-27", "load": 0, "ctl": 32.2, "atl": 39.4, "form": -7.2}],
                "planned": [{"date": "2026-10-28", "ctl": 33.1, "atl": 44.0}],
                "blocks": [{"id": "block0", "name": "Re-entry", "start": "2026-10-14", "end": "2026-10-25", "status": "designed", "ctl_end": None},
                           {"id": "block1", "name": "Accumulation", "start": "2026-10-26", "end": "2026-11-22", "status": "designed", "ctl_end": [75.0, 85.0]}],
                "events": [{"date": "2027-04-17", "label": "solo-312", "priority": "A"},
                           {"date": "2026-11-23", "end": "2026-11-29", "label": "athx", "priority": "C"},
                           {"date": "2027-02-01", "label": "hyrox", "priority": "C", "month": True}],
                "seed": {"date": "2026-10-07", "ctl": 30.8, "atl": 48.1},
            },
            "weekly_load": [{"week": "2026-10-19", "by_sport": {"bike": 253, "strength": 19}, "total": 272, "planned": 355, "band": [300.0, 400.0], "future": False},
                            {"week": "2026-10-26", "by_sport": {"grinding": 40}, "total": 40, "planned": 693, "band": None, "future": False}],
            "bench": {"points": [{"date": "2026-10-19", "e1rm": 131.7, "set": "100 x 8 @8.5"}],
                      "checkpoints": [{"date": "2026-10-19", "e1rm": 131.5, "status": "met"}, {"date": "2027-04-17", "e1rm": 147.0, "status": "upcoming"}],
                      "goal": {"date": "2027-04-17", "e1rm_needed_kg": 147}},
            "lifts": {"trap_bar_deadlift": [{"date": "2026-10-21", "e1rm": 216.0, "set": "180 x 6"}], "bench_pull": []},
            "grinding": {"now": {"60s": 470.0, "240s": 301.0}, "target": {"60s": 500.0, "240s": 330.0},
                         "tests": [{"date": "2026-10-20", "duration": "60s", "watts": 480}, {"date": "2026-10-22", "duration": "60s", "watts": 476}]},
            "bike": {"ftp_now": 340.0, "ftp_intervals_icu": [{"date": "2026-10-18", "ftp": 310.0}], "ftp_target": [365.0, 380.0],
                     "long_rides": [{"date": "2026-10-22", "minutes": 188.3, "name": "Muro"}],
                     "late_ride_power": [{"date": "2026-10-22", "first_hour_w": 244.0, "last_hour_w": 228.0, "ratio": 0.934}]},
            "readiness": [{"date": "2026-10-26", "readiness": "green", "hrv": 54.0, "sleep_h": 7.4}, {"date": "2026-10-27", "readiness": "amber", "hrv": 30.0}],
            "compliance": [{"week": "2026-10-19", "done": 1, "missed": 1}],
            "body": {"weight": [{"date": "2026-10-22", "weight_kg": 116.0, "avg7_kg": 116.0}], "dexa": [{"date": "2026-10-01", "fat_pct": 22.0}]},
        },
    }  # fmt: skip


def today():
    return {
        "date": "2026-10-27", "status": "final", "status_reason": "", "generated_at": "2026-10-27T06:40+01:00",
        "readiness": {"colour": "amber", "reason": "amber: HRV 30 < 32.9"},
        "block": {"id": "block1", "name": "Accumulation", "week": "acc1", "design_pending": False},
        "sessions": [
            {"slot": "tue_am", "order": 0, "session": "handbike_intervals", "modality": "handbike", "summary": "Handbike 5x4",
             "duration_min": 60, "tss": 70, "readiness_exempt": False, "provisional_ftp": True,
             "steps": [{"kind": "warmup", "duration_s": 600, "target": "126-164 W*"},
                       {"kind": "repeat", "count": 5, "duration_s": 2100, "steps": [{"kind": "work", "duration_s": 240, "target": "265-277 W*"},
                                                                                   {"kind": "rest", "duration_s": 180, "target": "RPE 2", "note": "spin"}]}]},
            {"slot": "tue_pm", "order": 0, "session": "upper_me", "modality": "strength", "summary": "Upper max effort",
             "duration_min": None, "tss": 35, "readiness_exempt": False,
             "strength": [{"lift": "bench_press", "name": "Bench press", "role": "top", "sets": 1, "reps": 5, "kg": 112.5, "rpe_cap": 8, "basis": "history", "reason": ""},
                          {"lift": "bench_pull", "name": "Bench pull", "role": "accessory", "sets": 3, "reps": 8, "kg": None, "rpe_cap": None, "basis": "choose", "reason": ""}]},
        ],
        "pipeline": {"failed": ["publish"], "skipped": []},
    }  # fmt: skip


def debrief():
    return {
        "date": "2026-10-26", "verdict": "Held: upper max effort; Z2 on target.", "note_for_today": "Keep the handbike reps honest.",
        "items": [
            {"kind": "strength", "slot": "mon_am", "session": "upper_me", "modality": "strength", "summary": "Upper max effort", "planned_tss": 35,
             "lifts": [{"lift": "bench_press", "name": "Bench press", "sets": [{"role": "top", "set": 1, "prescribed": "110 x 5", "achieved": "110 x 5 @8", "status": "held"}]}],
             "grade": "held"},
            {"kind": "endurance", "slot": "mon_pm", "session": "z2_90", "modality": "bike", "summary": "Z2 90", "planned_min": 90, "planned_tss": 70,
             "activity": {"id": "i1", "name": "Evening ride", "moving_min": 92.0},
             "reps": [{"n": 1, "target": "221-245 W", "band": [221, 245], "watts": 232, "hr": 131, "status": "on"}],
             "grade": "on_target", "grade_note": ""},
            {"kind": "unplanned", "sport": "sail_or_other", "type": "Sail", "name": "Sailing", "moving_min": 120.0, "hr_load": 40.0, "avg_hr": 110.0, "review": ""},
        ],
    }  # fmt: skip


def analysis():
    return {
        "status": "ok", "generated_at": "2026-10-27T19:00+01:00",
        "activity": {"id": "i9", "date": "2026-10-27", "start_local": "2026-10-27T17:00:00", "sport": "bike", "type": "Ride", "name": "Threshold", "moving_min": "75.0"},
        "breakdown": {"kind": "endurance", "session": "bike_threshold_2x20", "grade": "on_target", "moving_min": 75.0, "np_w": 300.0, "intensity": 0.88,
                      "tss": 95.0, "avg_hr": 150.0, "hr_drift_pct": 3.0, "power_curve": {"60": 420, "240": 360, "1200": 330}},
        "fitness": {"session_load": 95.0, "load_source": "power", "grind_correction": 0.0,
                    "before": {"ctl": 32.2, "atl": 39.4, "form": -7.2}, "after": {"ctl": 33.7, "atl": 47.2, "form": -13.5},
                    "projections": {"form_tomorrow": -13.5, "next_key_session": {"date": "2026-10-30", "session": "bike_threshold_2x20", "summary": "Threshold 2x20", "form": -9.0}},
                    "week": {"start": "2026-10-26", "label": "acc1", "done_tss": 205, "band": [650.0, 750.0], "share_pct": [27, 32]}},
        "bests": [{"kind": "power", "sport": "bike", "duration": "20 min", "watts": 330, "previous": 361, "first": False},
                  {"kind": "power", "sport": "bike", "duration": "1 min", "watts": 420, "previous": None, "first": True}],
        "bests_since": "2026-10-25",
        "anchor_flags": [{"anchor": "ftp.bike", "current_w": 340.0, "suggested_w": 314, "change_pct": -7.6, "evidence": "20-min best 330 W x 0.95"}],
    }  # fmt: skip


@pytest.fixture
def rows():
    return ex.build(dashboard(), today(), debrief(), analysis())


def test_every_dataset_is_written_with_even_rows(rows):
    assert list(rows) == ["summary", "today_sessions", "latest_session", "yesterday_items", "fitness_days", "fitness_planned", "blocks", "events",
                          "weekly_load", "bench_points", "bench_checkpoints", "lifts", "grinding", "bike", "readiness_days", "compliance", "body"]  # fmt: skip
    for name, data in rows.items():
        assert isinstance(data, list), name
        assert all(set(r) == set(data[0]) for r in data), name
        assert all(not isinstance(v, (dict, list)) for r in data for v in r.values()), name


def test_summary(rows):
    (s,) = rows["summary"]
    assert (s["date"], s["status"], s["readiness"], s["readiness_reason"]) == ("2026-10-27", "final", "amber", "amber: HRV 30 < 32.9")
    assert (s["ftp_w"], s["ftp_target_lo_w"], s["ftp_target_hi_w"]) == (340.0, 365.0, 380.0)
    assert (s["goal_date"], s["days_to_goal"]) == ("2027-04-17", 172)
    assert (s["block"], s["week"]) == ("Accumulation", "acc1")
    assert s["yesterday_verdict"].startswith("Held") and s["note_for_today"] == "Keep the handbike reps honest."
    assert s["pipeline_failed"] == "publish"
    assert s["analysis_generated_at"] == "2026-10-27T19:00+01:00"
    assert s["exported_at"].endswith("Z")


def test_exported_at_is_utc():
    import datetime as dt

    now = dt.datetime(2026, 10, 27, 19, 30, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    (s,) = ex.build(dashboard(), today(), debrief(), None, now=now)["summary"]
    assert (s["exported_at"], s["analysis_generated_at"]) == ("2026-10-27T18:30:00Z", None)


def test_summary_without_a_readiness_call():
    t = today()
    t["readiness"] = {"colour": None, "reason": "no call: no HRV"}
    (s,) = ex.summary(dashboard(), t, debrief())
    assert s["readiness"] == "no call"


def test_today_sessions_detail(rows):
    hb, upper = rows["today_sessions"]
    assert hb["detail"] == "warmup 10:00 126-164 W*; 5 × [work 4:00 265-277 W*, rest 3:00 RPE 2 (spin)]"
    assert hb["provisional_ftp"] and not upper["provisional_ftp"]
    assert upper["detail"] == "Bench press 1×5 @ 112.5 kg RPE ≤8; Bench pull 3×8 @ choose kg"
    assert ex.today_sessions({"sessions": []}) == []


def test_latest_session(rows):
    (r,) = rows["latest_session"]
    assert (r["activity_id"], r["name"], r["np_w"], r["tss"], r["moving_min"]) == ("i9", "Threshold", 300.0, 95.0, 75.0)
    assert (r["ctl_before"], r["atl_before"], r["form_before"], r["ctl_after"], r["atl_after"], r["form_after"]) == (32.2, 39.4, -7.2, 33.7, 47.2, -13.5)
    assert (r["form_tomorrow"], r["next_key_date"], r["next_key_session"], r["next_key_form"]) == (-13.5, "2026-10-30", "Threshold 2x20", -9.0)
    assert (r["week_done_tss"], r["week_band_lo"], r["week_band_hi"]) == (205, 650.0, 750.0)
    assert (r["best_1min_w"], r["best_4min_w"], r["best_20min_w"]) == (420, 360, 330)
    assert r["bests"] == "20 min 330 W (was 361 W) · 1 min 420 W (first)"
    assert r["anchor_flags"] == "ftp.bike 340 → 314 W (-7.6%): 20-min best 330 W x 0.95"


@pytest.mark.parametrize("an", [None, {"status": "no_new_activity", "generated_at": "2026-10-27T19:00+01:00"}])
def test_latest_session_without_an_analysis(an):
    (r,) = ex.latest_session(an)
    assert r["status"] == (an or {}).get("status", "none")


def test_yesterday_items(rows):
    strength, ride, sail = rows["yesterday_items"]
    assert strength["detail"] == "Bench press: 110 x 5 @8 held" and strength["grade"] == "held"
    assert (ride["done_min"], ride["activity"], ride["detail"], ride["grade"]) == (92.0, "Evening ride", "232 W on", "on_target")
    assert (sail["kind"], sail["modality"], sail["done_min"], sail["grade"]) == ("unplanned", "sail_or_other", 120.0, "unplanned")
    assert all(r["date"] == "2026-10-26" for r in rows["yesterday_items"])


def test_fitness_blocks_and_events(rows):
    assert rows["fitness_days"][1]["icu_ctl"] is None  # filled so both rows have the same columns
    assert rows["fitness_planned"] == [{"date": "2026-10-28", "ctl": 33.1, "atl": 44.0}]
    assert [(b["id"], b["ctl_end_lo"], b["ctl_end_hi"]) for b in rows["blocks"]] == [("block0", None, None), ("block1", 75.0, 85.0)]
    assert [(e["label"], e["end"], e["month"]) for e in rows["events"]] == [("solo-312", None, False), ("athx", "2026-11-29", False), ("hyrox", None, True)]


def test_weekly_load_has_a_column_per_sport(rows):
    first, second = rows["weekly_load"]
    assert (first["load_bike"], first["load_strength"], first["load_grinding"], first["band_lo"], first["band_hi"]) == (253, 19, 0, 300.0, 400.0)
    assert (second["load_grinding"], second["band_lo"]) == (40, None)


def test_goals(rows):
    assert [c["e1rm"] for c in rows["bench_checkpoints"]] == [131.5, 147.0]
    assert rows["lifts"] == [{"lift": "trap_bar_deadlift", "date": "2026-10-21", "e1rm": 216.0, "set": "180 x 6"}]
    one, four = rows["grinding"]
    assert (one["duration"], one["now_w"], one["target_w"], one["best_test_w"], one["best_test_date"]) == ("1 min", 470.0, 500.0, 480, "2026-10-20")
    assert (four["duration"], four["now_w"], four["target_w"], four["best_test_w"]) == ("4 min", 301.0, 330.0, None)
    bike = {r["series"]: r for r in rows["bike"]}
    assert (bike["ftp_now"]["value"], bike["ftp_now"]["date"]) == (340.0, "2026-10-27")
    assert (bike["ftp_target_lo"]["value"], bike["ftp_target_hi"]["value"], bike["ftp_target_hi"]["date"]) == (365.0, 380.0, "2027-04-17")
    assert bike["late_ride_ratio"]["label"] == "244 → 228 W"


def test_week_rows(rows):
    assert [r["readiness"] for r in rows["readiness_days"]] == ["green", "amber"]
    assert rows["readiness_days"][1]["sleep_h"] is None
    assert rows["compliance"] == [{"week": "2026-10-19", "done": 1, "missed": 1}]
    assert [(b["kind"], b["weight_kg"], b["fat_pct"]) for b in rows["body"]] == [("weight", 116.0, None), ("dexa", None, 22.0)]


# ------------------------------------------------------------------ reading and writing


def write_sources(root, analysis_too=True):
    files = {"dashboard.json": dashboard(), "today.json": today(), "debrief.json": debrief()}
    if analysis_too:
        files["analysis/latest.json"] = analysis()
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(json.dumps(data), encoding="utf-8")


def test_cli_from_a_directory(tmp_path, capsys):
    write_sources(tmp_path / "derived")
    out = tmp_path / "rows"
    assert ex.main(["--src", str(tmp_path / "derived"), "--out", str(out)]) == 0
    assert sorted(p.stem for p in out.glob("*.json")) == sorted(ex.build(dashboard(), today(), debrief(), analysis()))
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))[0]["days_to_goal"] == 172
    assert "wrote 17 datasets" in capsys.readouterr().out


def test_missing_analysis_is_allowed_but_a_missing_dashboard_is_not(tmp_path, capsys):
    write_sources(tmp_path, analysis_too=False)
    assert ex.read_sources(tmp_path)["analysis/latest.json"] is None
    (tmp_path / "dashboard.json").unlink()
    assert ex.main(["--src", str(tmp_path), "--out", str(tmp_path / "rows")]) == 1
    assert "dashboard.json not found" in capsys.readouterr().err
    assert not (tmp_path / "rows").exists()


def test_reads_a_git_ref_through_git_show():
    files = {"dashboard.json": dashboard(), "today.json": today(), "debrief.json": debrief()}
    calls = []

    def git(cmd, **kw):
        calls.append(cmd)
        name = cmd[2].split(":", 1)[1]
        if name in files:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(files[name]), "")
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: path does not exist")

    s = ex.read_sources(ref="origin/data", git=git)
    assert calls[0] == ["git", "show", "origin/data:dashboard.json"]
    assert s["today.json"]["date"] == "2026-10-27" and s["analysis/latest.json"] is None


def test_a_no_new_activity_latest_falls_back_to_the_last_analysis(tmp_path):
    write_sources(tmp_path)
    full = analysis()
    (tmp_path / "analysis" / "2026-10-27-i9.json").write_text(json.dumps(full), encoding="utf-8")
    later = {"status": "no_new_activity", "generated_at": "2026-10-27T20:00+01:00", "activity_id": "i9", "last_analysis": "analysis/2026-10-27-i9.md"}
    (tmp_path / "analysis" / "latest.json").write_text(json.dumps(later), encoding="utf-8")
    s = ex.read_sources(tmp_path)
    assert s["analysis/latest.json"]["activity"]["id"] == "i9" and s["analysis/request"] == later
    rows = ex.build(s["dashboard.json"], s["today.json"], s["debrief.json"], s["analysis/latest.json"], request=s["analysis/request"])
    (r,) = rows["latest_session"]
    assert (r["status"], r["name"], r["tss"]) == ("ok", "Threshold", 95.0)
    (summary,) = rows["summary"]
    assert (summary["last_request_status"], summary["last_request_at"]) == ("no_new_activity", "2026-10-27T20:00+01:00")
    assert summary["analysis_generated_at"] == "2026-10-27T19:00+01:00"


def test_an_error_latest_without_last_analysis_uses_the_newest_file(tmp_path):
    write_sources(tmp_path)
    for name, n in (("2026-10-20-i1.json", "Old"), ("2026-10-27-i9.json", "Threshold")):
        a = analysis()
        a["activity"]["name"] = n
        (tmp_path / "analysis" / name).write_text(json.dumps(a), encoding="utf-8")
    (tmp_path / "analysis" / "latest.json").write_text(json.dumps({"status": "error", "generated_at": "2026-10-27T20:00", "error": "boom"}), encoding="utf-8")
    assert ex.read_sources(tmp_path)["analysis/latest.json"]["activity"]["name"] == "Threshold"


def test_no_earlier_analysis_keeps_the_status_row(tmp_path):
    write_sources(tmp_path, analysis_too=False)
    (tmp_path / "analysis").mkdir()
    (tmp_path / "analysis" / "latest.json").write_text(json.dumps({"status": "no_new_activity", "generated_at": "2026-10-27T20:00+01:00"}), encoding="utf-8")
    s = ex.read_sources(tmp_path)
    assert ex.latest_session(s["analysis/latest.json"])[0]["status"] == "no_new_activity"

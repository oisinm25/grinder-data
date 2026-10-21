import datetime as dt
import subprocess
import sys

import pytest
import yaml
from pydantic import ValidationError

from pipeline.common import load_yaml
from pipeline.plan.schema import Plan, format_errors, plan_warnings, validate_plan
from tests.conftest import ATHLETE, PLAN, ROOT

DELETE = object()


def mutate(data, path, value):
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


def errors_for(data, athlete) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        validate_plan(data, athlete)
    return format_errors(exc.value, data)


def assert_error(errors: list[str], field: str, fragment: str = "") -> None:
    """Some error is located at exactly `field` and mentions `fragment`."""
    matches = [e for e in errors if e.split(": ", 1)[0] == field and fragment.lower() in e.lower()]
    assert matches, f"no error at {field!r} mentioning {fragment!r}; got:\n" + "\n".join(errors)


# ------------------------------------------------------------------ the real plan


def test_repo_plan_is_valid(plan_data, athlete):
    plan = validate_plan(plan_data, athlete)
    assert isinstance(plan, Plan)
    assert plan.plan_id == "winter-2026-27"
    assert [b.id for b in plan.blocks[:2]] == ["block0", "block1"]
    assert all(b.status == "design_pending" for b in plan.blocks[2:])
    assert plan.progressions["bench_top_linear"].type == "linear"
    assert plan.sessions["upper_me"].exercises[0].groups[1].pct_of_top == 0.85


def test_repo_plan_warns_only_about_soft_rules(plan_data, athlete):
    plan = validate_plan(plan_data, athlete)
    assert plan_warnings(plan) == ["blocks[6].weekly_tss: missing for block 'transition'"]


def test_every_plan_lift_is_in_athlete(plan_data, athlete):
    plan = validate_plan(plan_data, athlete)
    used = {ex.lift for s in plan.sessions.values() for ex in s.exercises or []}
    used |= {line.lift for line in plan.strength_lines}
    assert used <= set(athlete.strength.lifts)


# ------------------------------------------------------------------ invalid plans

S = "sessions"
INVALID = [
    # top level
    ("schema version", ["schema_version"], 2, "schema_version", "1"),
    ("missing plan_id", ["plan_id"], DELETE, "plan_id", "required"),
    ("unknown top-level key", ["notes"], "x", "notes", "extra"),
    ("valid_from not first block start", ["valid_from"], dt.date(2026, 10, 13), "valid_from", "first block"),
    ("valid_to not last block end", ["valid_to"], dt.date(2027, 4, 18), "valid_to", "last block"),
    ("no sessions", ["sessions"], {}, "sessions", ""),
    # goals and strength lines
    ("goal priority", ["goals", 0, "priority"], "D", "goals[0].priority", ""),
    ("goal date and window", ["goals", 0, "window"], [dt.date(2027, 4, 1), dt.date(2027, 4, 17)], "goals[0].window", "not both"),
    ("goal window reversed", ["goals", 2, "window"], [dt.date(2026, 11, 29), dt.date(2026, 11, 23)], "goals[2].window", "above"),
    ("goal unknown lift", ["goals", 1, "target", "lift"], "squat", "goals[1].target.lift", "athlete.yaml"),
    ("duplicate goal id", ["goals", 1, "id"], "solo-312", "goals[1].id", "duplicate"),
    ("strength line unknown lift", ["strength_lines", 0, "lift"], "bench", "strength_lines[0].lift", "unknown lift"),
    ("checkpoints out of order", ["strength_lines", 0, "checkpoints", 1, 0], dt.date(2026, 10, 1), "strength_lines[0].checkpoints", "ascending"),
    # progressions
    ("progression type", ["progressions", "bench_top_linear", "type"], "linearish", "progressions.bench_top_linear", "type"),
    ("linear missing increment", ["progressions", "bench_top_linear", "increment_kg"], DELETE, "progressions.bench_top_linear.increment_kg", "required"),
    ("linear extra field", ["progressions", "bench_top_linear", "rep_range"], [5, 8], "progressions.bench_top_linear.rep_range", "extra"),
    ("double rep_range reversed", ["progressions", "pull_double", "rep_range"], [8, 5], "progressions.pull_double.rep_range", "above"),
    ("max_rpe over 10", ["progressions", "bench_top_linear", "max_rpe"], 11, "progressions.bench_top_linear.max_rpe", ""),
    ("wave pct over 1", ["progressions", "bench_speed_wave", "pcts", 0], 1.5, "progressions.bench_speed_wave.pcts[0]", ""),
    # calendar
    ("block ends before start", ["blocks", 2, "end"], dt.date(2026, 11, 1), "blocks[2].end", "before it starts"),
    ("blocks overlap", ["blocks", 2, "start"], dt.date(2026, 11, 20), "blocks[2].start", "overlaps"),
    ("duplicate block id", ["blocks", 3, "id"], "athx", "blocks[3].id", "duplicate"),
    ("block status", ["blocks", 2, "status"], "pending", "blocks[2].status", ""),
    ("designed block without weeks", ["blocks", 0, "weeks"], DELETE, "blocks[0].weeks", "designed block"),
    ("block weekly_tss reversed", ["blocks", 1, "weekly_tss"], [850, 650], "blocks[1].weekly_tss", "above"),
    ("week outside block", ["blocks", 1, "weeks", 3, "start"], dt.date(2026, 11, 23), "blocks[1].weeks[3].start", "outside"),
    ("weeks out of order", ["blocks", 1, "weeks", 1, "start"], dt.date(2026, 11, 10), "blocks[1].weeks[2].start", "order"),
    ("first week late", ["blocks", 1, "weeks", 0, "start"], dt.date(2026, 10, 27), "blocks[1].weeks[0].start", "block start"),
    ("unknown week template", ["blocks", 1, "weeks", 0, "template"], "accumulaton", "blocks[1].weeks[0].template", "unknown"),
    ("swap unknown slot", ["blocks", 1, "weeks", 1, "swap", "sat_noon"], "z2_60", "blocks[1].weeks[1].swap.sat_noon", ""),
    ("swap unknown session", ["blocks", 1, "weeks", 1, "swap", "tue_am"], ["handbike_intervals", "grinding_repeats_99"], "blocks[1].weeks[1].swap.tue_am", "grinding_repeats_99"),
    ("template unknown session", ["week_templates", "accumulation", "mon_am"], "upper_max", "week_templates.accumulation.mon_am", "unknown session"),
    ("template unknown slot", ["week_templates", "accumulation", "mon_eve"], "z2_60", "week_templates.accumulation.mon_eve", ""),
    # sessions
    ("modality", [S, "z2_60", "modality"], "cycling", "sessions.z2_60.modality", ""),
    ("missing summary", [S, "z2_60", "summary"], DELETE, "sessions.z2_60.summary", "required"),
    ("environment", [S, "long_ride_3h", "environment"], "garage", "sessions.long_ride_3h.environment", ""),
    ("endurance without steps", [S, "z2_60", "steps"], DELETE, "sessions.z2_60.steps", "required for modality 'bike'"),
    ("strength with steps", [S, "upper_me", "steps"], [{"kind": "steady", "duration_s": 60, "target": {"rpe": 5}}], "sessions.upper_me.steps", "not allowed"),
    ("strength without exercises", [S, "upper_me", "exercises"], DELETE, "sessions.upper_me.exercises", "required"),
    ("rest with exercises", [S, "rest", "exercises"], [{"lift": "bench_press", "groups": [{"role": "work", "sets": 1, "reps": 5}]}], "sessions.rest.exercises", "not allowed"),
    ("unknown session target", [S, "z2_60", "targets", "if"], 0.7, "sessions.z2_60.targets.if", "extra"),
    ("steps_held_pct over 1", [S, "z2_60", "targets", "steps_held_pct"], 1.5, "sessions.z2_60.targets.steps_held_pct", ""),
    # steps
    ("two anchors", [S, "z2_60", "steps", 1, "target", "watts"], [200, 220], "sessions.z2_60.steps[1].target", "exactly one anchor"),
    ("no anchor", [S, "z2_60", "steps", 1, "target"], {"cadence": [85, 95]}, "sessions.z2_60.steps[1].target", "got none"),
    ("hr_zone 6", [S, "z2_60", "steps", 1, "target"], {"hr_zone": 6}, "sessions.z2_60.steps[1].target.hr_zone", ""),
    ("pct_ftp reversed", [S, "z2_60", "steps", 1, "target", "pct_ftp"], [0.72, 0.65], "sessions.z2_60.steps[1].target.pct_ftp", "above"),
    ("unknown anchor", [S, "z2_60", "steps", 1, "target", "pace"], "4:30", "sessions.z2_60.steps[1].target.pace", "extra"),
    ("unknown step kind", [S, "z2_60", "steps", 0, "kind"], "sprint", "sessions.z2_60.steps[0]", "kind"),
    ("missing duration", [S, "z2_60", "steps", 0, "duration_s"], DELETE, "sessions.z2_60.steps[0].duration_s", "required"),
    ("repeat count 0", [S, "bike_threshold_2x20", "steps", 1, "count"], 0, "sessions.bike_threshold_2x20.steps[1].count", ""),
    ("nested step error", [S, "bike_threshold_2x20", "steps", 1, "steps", 0, "duration_s"], -5, "sessions.bike_threshold_2x20.steps[1].steps[0].duration_s", ""),
    ("unknown curve point", [S, "grinding_repeats_8", "steps", 1, "steps", 0, "target", "pct_curve"], ["grinding_30s", 0.85], "sessions.grinding_repeats_8.steps[1].steps[0].target.pct_curve", "grinding_30s"),
    ("pct_ftp without that FTP", [S, "z2_60", "modality"], "run", "sessions.z2_60.steps[0].target.pct_ftp", "no run FTP"),
    # exercises
    ("unknown lift", [S, "upper_me", "exercises", 0, "lift"], "bench", "sessions.upper_me.exercises[0].lift", "athlete.yaml"),
    ("unknown progression", [S, "upper_me", "exercises", 0, "progression"], "bench_top", "sessions.upper_me.exercises[0].progression", "unknown progression"),
    ("group role", [S, "upper_me", "exercises", 0, "groups", 0, "role"], "heavy", "sessions.upper_me.exercises[0].groups[0].role", ""),
    ("backoff without pct_of_top", [S, "upper_me", "exercises", 0, "groups", 1, "pct_of_top"], DELETE, "sessions.upper_me.exercises[0].groups[1].pct_of_top", "backoff"),
    ("pct_of_top on a top set", [S, "upper_me", "exercises", 0, "groups", 0, "pct_of_top"], 0.9, "sessions.upper_me.exercises[0].groups[0].pct_of_top", "only a backoff"),
    ("backoff without top", [S, "upper_me", "exercises", 0, "groups"], [{"role": "backoff", "sets": 3, "reps": 8, "pct_of_top": 0.85}], "sessions.upper_me.exercises[0].groups", "top group"),
    ("zero reps", [S, "upper_me", "exercises", 0, "groups", 0, "reps"], 0, "sessions.upper_me.exercises[0].groups[0].reps", ""),
    ("rep range without double", [S, "upper_me", "exercises", 0, "groups", 0, "reps"], [5, 6], "sessions.upper_me.exercises[0].groups[0].reps", "double progression"),
    ("double reps not rep_range", [S, "upper_re", "exercises", 0, "groups", 0, "reps"], [8, 10], "sessions.upper_re.exercises[0].groups[0].reps", "rep_range"),
    ("negative start_kg", [S, "upper_me", "exercises", 0, "start_kg"], -100, "sessions.upper_me.exercises[0].start_kg", ""),
    ("no groups", [S, "upper_me", "exercises", 0, "groups"], [], "sessions.upper_me.exercises[0].groups", ""),
]


@pytest.mark.parametrize(("path", "value", "field", "fragment"), [c[1:] for c in INVALID], ids=[c[0] for c in INVALID])
def test_invalid_plan_names_the_field(plan_data, athlete, path, value, field, fragment):
    assert_error(errors_for(mutate(plan_data, path, value), athlete), field, fragment)


def test_reference_errors_are_all_reported(plan_data, athlete):
    mutate(plan_data, [S, "upper_me", "exercises", 0, "lift"], "bench")
    mutate(plan_data, ["week_templates", "accumulation", "mon_am"], "upper_max")
    errors = errors_for(plan_data, athlete)
    assert len(errors) == 2


def test_lift_must_exist_in_athlete(plan_data, athlete):
    lifts = dict(athlete.strength.lifts)
    del lifts["push_press"]
    smaller = athlete.model_copy(update={"strength": athlete.strength.model_copy(update={"lifts": lifts})})
    with pytest.raises(ValidationError) as exc:
        validate_plan(plan_data, smaller)
    assert format_errors(exc.value, plan_data) == [
        "sessions.upper_de.exercises[2].lift: unknown lift 'push_press' (not in athlete.yaml strength.lifts)"
    ]


def test_duplicate_yaml_key_is_refused(tmp_path):
    p = tmp_path / "plan.yaml"
    p.write_text("sessions:\n  z2_60: {modality: rest, summary: a}\n  z2_60: {modality: rest, summary: b}\n")
    with pytest.raises(yaml.YAMLError, match="duplicate key 'z2_60'"):
        load_yaml(p)


# ------------------------------------------------------------------ CLI


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "pipeline.plan.schema", *map(str, args)],
        cwd=ROOT, capture_output=True, text=True,
    )


def test_cli_ok():
    r = run_cli("config/plan.yaml")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK - plan winter-2026-27" in r.stdout


def test_cli_invalid_names_field(tmp_path, plan_data):
    mutate(plan_data, [S, "upper_me", "exercises", 0, "groups", 1, "pct_of_top"], DELETE)
    bad = tmp_path / "plan.yaml"
    bad.write_text(yaml.safe_dump(plan_data, sort_keys=False))
    r = run_cli(bad, "--athlete", ATHLETE)
    assert r.returncode == 1
    assert "INVALID - 1 error(s)" in r.stdout
    assert "sessions.upper_me.exercises[0].groups[1].pct_of_top: Value error, required for a backoff group" in r.stdout


def test_cli_invalid_athlete(tmp_path, athlete_data):
    del athlete_data["readiness"]["red"]["rpe_cap"]
    (tmp_path / "athlete.yaml").write_text(yaml.safe_dump(athlete_data))
    (tmp_path / "plan.yaml").write_text(PLAN.read_text(encoding="utf-8"), encoding="utf-8")
    r = run_cli(tmp_path / "plan.yaml")
    assert r.returncode == 1
    assert "readiness.red.rpe_cap: Field required" in r.stdout


def test_cli_missing_file():
    r = run_cli("config/nope.yaml")
    assert r.returncode == 2
    assert "error:" in r.stderr

"""python -m pipeline.coach.snapshot [--rows derived/rows] [--ref origin/data] [--out derived/coach_snapshot.json]

Builds Pedestal Coach's data document, `pedestal/snapshot` in its store: {rows, exported_at,
data_commit, files}. `rows` are export_rows' datasets (one list per dataset), `files` the text the
coach reads beside them: recent CSVs and briefs from the data branch, the latest analyses, and
athlete.yaml and the training notes from main. CSVs keep their header and last rows, and the whole
document stays under the store's size limit. Claude writes it to the store (README.md).
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

DATA_FILES = ("today.md", "debrief.md", "week.md", "activities.csv", "activity_metrics.csv", "compliance.csv",
              "prescriptions.csv", "strength.csv", "weekly.csv", "wellness.csv")  # fmt: skip
REPO_FILES = ("config/athlete.yaml", "training/goals.md", "training/blocks.md", "training/design-rules.md")
CSV_TAIL = 120  # data rows kept per CSV
ANALYSES = 3  # latest analysis/*.md
LIMIT = 900_000  # bytes of JSON; the store refuses documents near 1 MB


def _git_show(ref: str, path: str, run) -> str | None:
    r = run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def tail_csv(text: str, n: int = CSV_TAIL) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:1] + lines[1:][-n:]) + "\n" if lines else ""


def build(rows: dict[str, list], *, ref: str, now: dt.datetime, repo: Path = Path("."), run=subprocess.run) -> dict:
    files: dict[str, str] = {}
    for name in DATA_FILES:
        text = _git_show(ref, name, run)
        if text is not None:
            files[name] = tail_csv(text) if name.endswith(".csv") else text
    ls = run(["git", "ls-tree", "--name-only", f"{ref}:analysis"], capture_output=True, text=True, encoding="utf-8")
    names = sorted(n for n in (ls.stdout.split() if ls.returncode == 0 else []) if n[:4].isdigit() and n.endswith(".md"))
    for n in names[-ANALYSES:]:
        text = _git_show(ref, f"analysis/{n}", run)
        if text is not None:
            files[f"analysis/{n}"] = text
    for name in REPO_FILES:
        p = repo / name
        if p.exists():
            files[name] = p.read_text(encoding="utf-8")
    sha = run(["git", "rev-parse", "--short", ref], capture_output=True, text=True, encoding="utf-8")
    doc = {"rows": rows, "exported_at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "data_commit": sha.stdout.strip() if sha.returncode == 0 else None, "files": files}  # fmt: skip
    # over the limit: drop the oldest analyses, then shorten the CSVs, until it fits
    while len(json.dumps(doc)) > LIMIT:
        old = sorted(k for k in files if k.startswith("analysis/"))
        if old:
            del files[old[0]]
            continue
        csvs = [k for k in files if k.endswith(".csv") and files[k].count("\n") > 11]
        if not csvs:
            break
        for k in csvs:
            files[k] = tail_csv(files[k], max(10, (files[k].count("\n") - 1) // 2))
    return doc


def read_rows(path: Path) -> dict[str, list]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))}


def main(argv=None, *, run=subprocess.run, now=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.coach.snapshot", description=__doc__.split("\n\n")[1])
    parser.add_argument("--rows", type=Path, default=Path("derived/rows"))
    parser.add_argument("--ref", default="origin/data")
    parser.add_argument("--out", type=Path, default=Path("derived/coach_snapshot.json"))
    args = parser.parse_args(argv)
    rows = read_rows(args.rows)
    if not rows:
        print(f"snapshot: no rows in {args.rows}; run export_rows first", file=sys.stderr)
        return 1
    doc = build(rows, ref=args.ref, now=(now or dt.datetime.now(dt.timezone.utc)), run=run)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}: {len(rows)} datasets, {len(doc['files'])} files, {len(json.dumps(doc)) // 1024} KB, data {doc['data_commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

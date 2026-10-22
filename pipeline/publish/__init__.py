"""Publish: the next days of the plan to the intervals.icu calendar (which forwards to Garmin
and Zwift), plus fallback .zwo and .fit workout files in exports/workouts/.

    python -m pipeline.publish --from 2026-10-26 --days 7          # dry run: files only
    python -m pipeline.publish --from 2026-10-26 --days 7 --push   # write to intervals.icu
"""

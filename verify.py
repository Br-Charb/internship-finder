"""Read-only live source check. It never touches AWS or Discord."""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from jobfinder.match import is_target_job
from jobfinder.sources import fetch_all


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate configured ATS endpoints without sending messages.")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SOURCE_NAME",
        help="check one exact source name; repeat for multiple sources",
    )
    parser.add_argument(
        "--tier",
        action="append",
        type=int,
        choices=(1, 2, 3),
        help="check only this tier; repeat for multiple tiers (default: all)",
    )
    parser.add_argument(
        "--strict-zero",
        action="store_true",
        help="return a failure code when a reachable source currently has zero jobs",
    )
    args = parser.parse_args()

    config = tomllib.loads((ROOT / "sources.toml").read_text(encoding="utf-8"))
    jobs, statuses = fetch_all(
        config,
        tiers=set(args.tier or (1, 2, 3)),
        only=set(args.only) or None,
    )
    selected = {name.casefold() for name in args.only}
    found_names = {str(status["name"]).casefold() for status in statuses}
    missing_names = sorted(name for name in selected if name not in found_names)
    eligible = sum(
        is_target_job(
            str(job.get("title") or ""),
            employment_type=str(job.get("employment_type") or ""),
        )
        for job in jobs
    )
    failures = [status for status in statuses if not status["ok"]]
    zero_sources = [status["name"] for status in statuses if status["ok"] and status["count"] == 0]
    summary = {
        "sources_checked": len(statuses),
        "sources_failed": len(failures),
        "sources_with_zero_jobs": zero_sources,
        "total_jobs": len(jobs),
        "internship_style_jobs": eligible,
        "unknown_requested_names": missing_names,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures or missing_names or (args.strict_zero and zero_sources) else 0


if __name__ == "__main__":
    raise SystemExit(main())

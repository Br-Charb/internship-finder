"""Daily aerospace internship scan.

Run locally with:   python run.py
GitHub Actions runs this every morning and publishes docs/index.html.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jobfinder.ai import review_jobs
from jobfinder.match import Matcher
from jobfinder.quals import QualChecker
from jobfinder.render import build_page, update_readme
from jobfinder.sources import fetch_all, fetch_description
from jobfinder.util import FetchError, job_key

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOBS_FILE = DATA / "jobs.json"
DESC_FILE = DATA / "descriptions.json"
AI_FILE = DATA / "ai_reviews.json"
PAGE_URL = "https://jonasagront.github.io/aero-job-finder/"

MAX_NEW_LOOKUPS = 500   # full-description lookups per run for list-only postings
KEEP_MISSING_DAYS = 5   # keep a job this long if its source failed to load


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    profile = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    sources_cfg = tomllib.loads((ROOT / "sources.toml").read_text(encoding="utf-8"))
    matcher = Matcher(profile, sources_cfg)
    matcher.qc = QualChecker(matcher, profile)

    print("1) Reading job sources")
    raw, status = fetch_all(sources_cfg)
    print(f"   {len(raw)} postings read")

    # 2) Quick title filter, then de-duplicate across sources
    candidates: dict[str, dict] = {}
    for j in raw:
        ok, _ = matcher.title_ok(j.get("title", ""))
        if not ok or not j.get("url"):
            continue
        j["key"] = job_key(j["url"], j.get("company", ""), j.get("title", ""))
        prev = candidates.get(j["key"])
        # Prefer the copy that has a description (company board over list)
        if prev is None or (not prev.get("description") and j.get("description")):
            if prev and prev.get("season_hint") and not j.get("season_hint"):
                j["season_hint"] = prev["season_hint"]
            candidates[j["key"]] = j
        elif j.get("season_hint") and not prev.get("season_hint"):
            prev["season_hint"] = j["season_hint"]
    print(f"2) {len(candidates)} internship-titled postings after removing duplicates")

    # 3) Fill in descriptions for list postings (cached between runs)
    desc_cache: dict = load_json(DESC_FILE, {})
    need = [j for j in candidates.values() if not j.get("description")]
    for j in need:
        if j["key"] in desc_cache:
            j["description"] = desc_cache[j["key"]]
    lookup = [j for j in need if not j.get("description")]
    # Look up the most promising ones first (aerospace words in title / aerospace company)
    lookup.sort(key=lambda j: (not matcher.is_aero_company(j.get("company", "")),))
    lookup = lookup[:MAX_NEW_LOOKUPS]

    def get_desc(j):
        try:
            return j, fetch_description(j["url"])
        except (FetchError, ValueError):
            return j, ""

    got = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for j, text in pool.map(get_desc, lookup):
            if text:
                j["description"] = text
                desc_cache[j["key"]] = text[:12000]
                got += 1
    print(f"3) Loaded {got} full descriptions ({len(lookup)} tried)")

    # 4) Score
    drops: dict[str, int] = {}
    scored = []
    descriptions = {}
    for j in candidates.values():
        analysis, why = matcher.analyze(j)
        if analysis is None:
            drops[why] = drops.get(why, 0) + 1
            continue
        descriptions[j["key"]] = j.get("description", "")
        scored.append({k: v for k, v in j.items() if k not in ("description", "season_hint")} | analysis)
    print(f"4) {len(scored)} matches. Dropped: " + ", ".join(f"{v} {k}" for k, v in sorted(drops.items())))

    # 5) Merge with yesterday's results (keeps "first seen" dates)
    old = {j["key"]: j for j in load_json(JOBS_FILE, {}).get("jobs", [])}
    failed_sources = {s["name"] for s in status if not s["ok"]}
    final = []
    for j in scored:
        j["first_seen"] = old.get(j["key"], {}).get("first_seen", today)
        j["last_seen"] = today
        final.append(j)
    current = {j["key"] for j in final}
    for k, j in old.items():
        if k in current or j.get("source_id") not in failed_sources:
            continue  # gone from a working source = posting closed
        last = dt.date.fromisoformat(j.get("last_seen", today))
        if (now.date() - last).days <= KEEP_MISSING_DAYS:
            final.append(j)  # source hiccup: keep for now

    # 6) Optional Claude reviews
    ai_cache = load_json(AI_FILE, {})
    n = review_jobs(final, descriptions, profile, ai_cache)
    if n:
        print(f"6) Claude reviewed {n} postings")
    for j in final:
        if j["key"] in ai_cache:
            j["ai"] = ai_cache[j["key"]]

    final.sort(key=lambda j: (-j["score"], j["company"]))

    # 7) Save + publish
    keep = {j["key"] for j in final}
    save_json(JOBS_FILE, {"updated": now.isoformat(timespec="seconds"), "jobs": final})
    save_json(DESC_FILE, {k: v for k, v in desc_cache.items() if k in keep or k in candidates})
    save_json(AI_FILE, {k: v for k, v in ai_cache.items() if k in keep})
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text(build_page(final, status, profile, now), encoding="utf-8")
    update_readme(ROOT / "README.md", final, PAGE_URL, now,
                  fresh_days=int(profile["search"].get("fresh_days", 30)))
    new_today = sum(1 for j in final if j["first_seen"] == today)
    print(f"Done: {len(final)} matches on the page, {new_today} new today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Optional: ask Claude for a short, honest fit review of top matches.

Runs only when the ANTHROPIC_API_KEY secret exists. Each job is reviewed
once and cached in data/ai_reviews.json, so the daily cost stays small.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .util import FetchError, post_json


def profile_summary(profile: dict) -> str:
    st = profile["student"]
    skills = "; ".join(f"{s['name']} (strength {s.get('weight', 1)}/3)" for s in profile.get("skill", []))
    return (
        f"{st['major']} student at {st.get('school', 'a university')}, graduating May {st['graduation_year']} "
        f"(a rising junior during Summer 2027). Experience: Formula SAE aerodynamics team "
        f"(2D CFD parametric studies of a multi-element rear wing in STAR-CCM+, carbon-fiber wing "
        f"fabrication with hand layup and vacuum bagging); designed, built, and flew a high-power rocket "
        f"for NAR Level 1 with OpenRocket, SolidWorks, and a RocketPy flight simulation; submersible rover "
        f"(top 5% of 481 teams); Arduino/C++ autonomous robot. Skills: {skills}."
    )


def profile_hash(profile: dict) -> str:
    return hashlib.sha1(profile_summary(profile).encode()).hexdigest()[:10]


PROMPT = """You are helping a college student decide which internships to apply to.

STUDENT
{profile}

POSTING
Company: {company}
Title: {title}
Location: {location}
Description:
{description}

Judge how well this student fits the posting's actual requirements. Be honest and specific;
a sophomore with project experience is competitive for many internships but not ones that
need senior standing or graduate study. Respond with ONLY a JSON object, no other text:
{{"fit": "strong" | "good" | "stretch" | "poor",
  "summary": "one sentence on why, naming the specific requirement or experience that decides it",
  "talking_point": "one resume item the student should lead with in this application"}}"""


def review_jobs(jobs: list[dict], descriptions: dict, profile: dict, cache: dict) -> int:
    cfg = profile.get("ai", {})
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cfg.get("enabled", True):
        return 0
    ph = profile_hash(profile)
    limit = int(cfg.get("max_reviews_per_run", 20))
    floor = int(cfg.get("min_score_to_review", 55))
    todo = [j for j in sorted(jobs, key=lambda j: -j["score"])
            if j["score"] >= floor and descriptions.get(j["key"])
            and cache.get(j["key"], {}).get("profile") != ph][:limit]
    done = 0
    for j in todo:
        prompt = PROMPT.format(profile=profile_summary(profile), company=j["company"], title=j["title"],
                               location=j["location"], description=descriptions[j["key"]][:6000])
        try:
            resp = post_json("https://api.anthropic.com/v1/messages", {
                "model": cfg.get("model", "claude-haiku-5-5"),
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            }, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=60, retries=1)
            text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            data = json.loads(text)
            cache[j["key"]] = {"profile": ph, "fit": str(data.get("fit", ""))[:10],
                               "summary": str(data.get("summary", ""))[:400],
                               "talking_point": str(data.get("talking_point", ""))[:300]}
            done += 1
        except (FetchError, ValueError, KeyError) as e:
            print(f"  AI review skipped for {j['company']} / {j['title']}: {e}")
            if "401" in str(e) or "403" in str(e):
                print("  Check your ANTHROPIC_API_KEY secret.")
                break
    return done

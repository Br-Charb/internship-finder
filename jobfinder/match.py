"""Decide whether a posting is a Summer 2027 aerospace internship, and how
well it fits your resume.

Score out of 100:
  up to 40  how clearly it's aerospace engineering work
  up to 35  how many of your skills the posting asks for
  up to 25  eligibility (season, class year, grad date, degree level)
"""
from __future__ import annotations

import datetime as dt
import re

from .util import compile_terms, hits

INTERN_RX = re.compile(r"(?i)(?<![a-z])(intern|interns|internship|internships|co-?op|co op|student trainee|student)(?![a-z])")

SEASON_WORDS = r"(summer|fall|autumn|spring|winter)"
SEASON_RX = re.compile(rf"(?i)(?<![a-z]){SEASON_WORDS}\s*(?:of\s*)?'?(20)?(2[5-9]|3[0-1])(?![0-9])"
                       rf"|(?<![0-9])(20)?(2[5-9]|3[0-1])\s*{SEASON_WORDS}")
MONTH_START_RX = re.compile(r"(?i)(may|june)\s+(?:\d{1,2},?\s+)?(20\d\d)")
GRAD_RX = re.compile(r"(?i)graduat\w*")
YEAR_RX = re.compile(r"(?<![0-9])(20[2-3]\d)(?![0-9])")
GPA_RX = re.compile(
    r"(?i)(?:gpa|grade point average)[^.\n]{0,40}?([2-4]\.\d{1,2})"
    r"|([2-4]\.\d{1,2})\s*(?:/\s*4\.0\s*)?(?:cumulative\s*|overall\s*|minimum\s*)?(?:gpa|grade point)")
CITIZEN_RX = re.compile(
    r"(?i)u\.?\s?s\.?\s+citizen|united states citizen|u\.?\s?s\.?\s+person|\bitar\b|"
    r"export[- ]control|lawful permanent resident|green card|citizenship is required")
CLEARANCE_RX = re.compile(
    r"(?i)security clearance|secret clearance|top secret|ts/sci|obtain (?:and maintain )?(?:a |an )?"
    r"(?:dod |government )?clearance|clearance (?:is )?required")
GRAD_DEGREE_ONLY_RX = re.compile(
    r"(?i)(pursuing|enrolled in) an? (master'?s|ph\.?d|doctoral|graduate) (degree|program)"
    r"(?! or)(?!.{0,30}bachelor)|graduate students only|ph\.?d\.? (candidate|student)s? only")
SENIOR_ONLY_RX = re.compile(r"(?i)rising senior|senior standing|senior[- ]level student|final[- ]year student")
JUNIOR_RX = re.compile(r"(?i)(?<![a-z])(rising )?juniors?(?![a-z])")
UNDERCLASS_RX = re.compile(r"(?i)sophomore|freshm[ae]n|first[- ]year|underclass|all (class )?(levels|years)|any (class )?(level|year)")
COMPLETED_YEARS_RX = re.compile(r"(?i)complet\w* (?:at least |a minimum of )?(one|two|three|four|1|2|3|4) (?:full )?(?:academic )?years?")
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "1": 1, "2": 2, "3": 3, "4": 4}


class Matcher:
    def __init__(self, profile: dict, sources_cfg: dict):
        self.p = profile
        st = profile["student"]
        se = profile["search"]
        self.grad_year = int(st["graduation_year"])
        self.gpa = float(st.get("gpa") or 0)
        self.season = se.get("season", "summer").lower()
        self.year = int(se.get("year", 2027))
        self.keep_unstated = bool(se.get("include_unstated_season", True))
        self.min_score = int(se.get("min_score", 40))
        self.max_age = int(se.get("max_age_days", 150))
        # Years of school finished by the internship summer (2029 grad -> 2 -> rising junior)
        self.completed_years = max(0, 4 - (self.grad_year - self.year))

        rel = profile["relevance"]
        self.t_strong = compile_terms(rel["title_strong"])
        self.t_medium = compile_terms(rel["title_medium"])
        self.t_weak = compile_terms(rel["title_weak"])
        self.text_kw = compile_terms(rel["text_keywords"])
        self.majors = {k.lower(): v for k, v in rel.get("majors", {}).items()}
        self.exclude = compile_terms(rel.get("exclude", {}).get("title", []))
        self.exclude_always = compile_terms(rel.get("exclude", {}).get("title_always", []))

        self.skills = []
        for s in profile.get("skill", []):
            self.skills.append({
                "name": s["name"], "weight": int(s.get("weight", 1)),
                "aliases": compile_terms(s.get("aliases", [])),
                "related": compile_terms(s.get("related", [])),
            })
        self.gaps = [{"name": g["name"], "aliases": compile_terms(g.get("aliases", []))}
                     for g in profile.get("gap", [])]
        self.aero_companies = [n.lower() for n in sources_cfg.get("aerospace_companies", {}).get("names", [])]

    # ---------------------------------------------------------- quick filter

    def title_ok(self, title: str) -> tuple[bool, str]:
        """Cheap check before fetching anything else."""
        t = title.lower()
        if not INTERN_RX.search(t):
            return False, "not an internship"
        if hits(self.exclude_always, t):
            return False, "graduate-student role"
        if hits(self.exclude, t) and not hits(self.t_strong, t):
            return False, "not an engineering role"
        return True, ""

    def is_aero_company(self, company: str) -> bool:
        c = company.lower()
        return any(n in c for n in self.aero_companies)

    # ---------------------------------------------------------- season

    def season_of(self, job: dict, text: str) -> tuple[str, str]:
        """Returns (status, label). status: match | unstated | other"""
        hint = (job.get("season_hint") or "").lower()
        target = f"{self.season} {self.year}"
        if hint:
            if target in hint:
                return "match", target.title()
            return "other", hint.title()
        found = set()
        for m in SEASON_RX.finditer(text):
            s1, _c1, y1, _c2, y2, s2 = m.groups()
            season = (s1 or s2 or "").lower().replace("autumn", "fall")
            year = 2000 + int(y1 or y2)
            found.add((season, year))
        for m in MONTH_START_RX.finditer(text):
            if int(m.group(2)) == self.year:
                found.add(("summer", self.year))
        if (self.season, self.year) in found:
            return "match", target.title()
        if found:
            # Only another season/year named, like "Fall 2026" or "Summer 2026"
            s, y = sorted(found, key=lambda x: (x[1], x[0]))[-1]
            return "other", f"{s.title()} {y}"
        if re.search(rf"(?i)(?<![a-z]){self.season}(?![a-z])", text):
            return "unstated", f"{self.season.title()} (year not stated)"
        return "unstated", "Season not stated"

    # ---------------------------------------------------------- eligibility

    def grad_check(self, text: str) -> tuple[bool, str] | None:
        """Look for graduation-date windows like 'graduating between Dec 2026 and May 2028'."""
        problems, oks = [], []
        for m in GRAD_RX.finditer(text):
            window = text[max(0, m.start() - 30): m.end() + 110]
            years = sorted({int(y) for y in YEAR_RX.findall(window)})
            years = [y for y in years if 2025 <= y <= 2032]
            if not years:
                continue
            w = window.lower()
            lo, hi = min(years), max(years)
            if re.search(r"or later|or after|and later|and beyond|after", w) and len(years) == 1:
                ok = self.grad_year >= lo
            elif re.search(r"or (before|earlier|sooner)|no later than|prior to|by ", w) and len(years) == 1:
                ok = self.grad_year <= hi
            elif len(years) >= 2:
                ok = lo <= self.grad_year <= hi
            else:
                ok = self.grad_year == lo
            label = f"{lo}" if lo == hi else f"{lo}\u2013{hi}"
            (oks if ok else problems).append(label)
        if problems:
            return False, problems[0]
        if oks:
            return True, oks[0]
        return None

    def class_check(self, text: str) -> tuple[str, str] | None:
        """Returns (kind, label) where kind is good | warn."""
        if UNDERCLASS_RX.search(text):
            return "good", "Open to underclassmen"
        m = COMPLETED_YEARS_RX.search(text)
        if m and WORDNUM[m.group(1).lower()] > self.completed_years:
            return "warn", f"Wants {WORDNUM[m.group(1).lower()]}+ years completed"
        if SENIOR_ONLY_RX.search(text) and not JUNIOR_RX.search(text):
            return "warn", "Seniors only?"
        return None

    # ---------------------------------------------------------- main

    def analyze(self, job: dict) -> tuple[dict | None, str]:
        """Returns (analysis, drop_reason). analysis is None when the job is dropped."""
        title = job.get("title", "")
        desc = job.get("description") or ""
        t = title.lower()
        d = desc.lower()
        full = f"{t}\n{d}"

        ok, why = self.title_ok(title)
        if not ok:
            return None, why

        # Too old?
        if job.get("posted"):
            try:
                age = (dt.date.today() - dt.date.fromisoformat(job["posted"][:10])).days
                if age > self.max_age:
                    return None, "posting too old"
            except ValueError:
                pass

        season_status, season_label = self.season_of(job, full)
        if season_status == "other":
            return None, "different season"
        if season_status == "unstated" and not self.keep_unstated:
            return None, "season not stated"

        # ---- relevance (0-40)
        strong, medium, weak = hits(self.t_strong, t), hits(self.t_medium, t), hits(self.t_weak, t)
        best = 3 if strong else 2 if medium else 1 if weak else 0
        text_hits = hits(self.text_kw, d)
        major_pts = max([v for k, v in self.majors.items() if k in d] or [0])
        aero_co = self.is_aero_company(job.get("company", ""))

        if desc:
            if best == 0 and len(text_hits) < 3 and major_pts < 6:
                return None, "not aerospace"
            if best == 1 and len(text_hits) < 2 and major_pts < 6 and not aero_co:
                return None, "not aerospace"
        else:
            if best == 0 or (best == 1 and not aero_co):
                return None, "not aerospace"

        relevance = {3: 22, 2: 14, 1: 6, 0: 0}[best]
        relevance += 4 * min(2, max(0, len(strong) + len(medium) - 1))
        relevance += min(10, 2 * len(text_hits))
        relevance += major_pts
        relevance += 6 if aero_co else 0
        relevance = min(40, relevance)

        # ---- skills (0-35)
        matched, related, gaps = [], [], []
        related_terms = set()
        if desc:
            weight = 0.0
            for s in self.skills:
                found = hits(s["aliases"], full)
                if found:
                    matched.append(s["name"])
                    # A posting that mentions a skill several ways leans on it hard
                    weight += s["weight"] * (1 + 0.5 * min(2, len(found) - 1))
                else:
                    rel = hits(s["related"], full)
                    if rel:
                        tool = rel[0].rstrip("*")
                        tool = tool.upper() if len(tool) <= 5 else tool.title()
                        related.append(f"{tool} (close to {s['name']})")
                        related_terms.update(r.rstrip("*") for r in rel)
                        weight += s["weight"] / 2
            for g in self.gaps:
                g_hits = hits(g["aliases"], d)
                if g_hits and not (set(h.rstrip("*") for h in g_hits) <= related_terms):
                    gaps.append(g["name"])
            skills_pts = min(35, round(weight * 3.5))
        else:
            skills_pts = 12  # unknown: neutral

        # ---- eligibility (0-25)
        flags = []
        elig = 25
        if season_status == "match":
            flags.append({"kind": "good", "label": season_label})
        else:
            flags.append({"kind": "info", "label": season_label})
            elig -= 3
        if desc:
            g = self.grad_check(desc)
            if g:
                ok, label = g
                if ok:
                    flags.append({"kind": "good", "label": f"Grad window {label} fits"})
                else:
                    flags.append({"kind": "warn", "label": f"Grad window {label}"})
                    elig -= 12
            c = self.class_check(desc)
            if c:
                flags.append({"kind": c[0], "label": c[1]})
                if c[0] == "warn":
                    elig -= 10
            if GRAD_DEGREE_ONLY_RX.search(desc):
                flags.append({"kind": "warn", "label": "Grad students only?"})
                elig -= 18
            gm = GPA_RX.search(desc)
            if gm:
                need = float(gm.group(1) or gm.group(2))
                if 2.0 <= need <= 4.0:
                    if self.gpa and self.gpa < need:
                        flags.append({"kind": "warn", "label": f"Min GPA {need:.1f}"})
                        elig -= 12
                    else:
                        flags.append({"kind": "info", "label": f"Min GPA {need:.1f}"})
            if CITIZEN_RX.search(desc) or job.get("source") == "USAJOBS":
                flags.append({"kind": "info", "label": "U.S. person / ITAR", "id": "citizen"})
            if CLEARANCE_RX.search(desc):
                flags.append({"kind": "info", "label": "Clearance", "id": "clearance"})
        else:
            flags.append({"kind": "info", "label": "Full description not loaded"})
        elig = max(0, elig)

        score = int(min(100, relevance + skills_pts + elig))
        if score < self.min_score:
            return None, "low score"
        tier = "strong" if score >= 72 else "good" if score >= 56 else "look"

        return {
            "score": score,
            "tier": tier,
            "parts": {"aerospace": relevance, "skills": skills_pts, "eligibility": elig},
            "matched": matched,
            "related": related,
            "gaps": gaps,
            "flags": flags,
            "season": season_status,
            "has_description": bool(desc),
        }, ""

"""Check a posting's qualification bullets, one by one, against your resume.

Finds the "Qualifications / Requirements / Preferred" sections of a description,
then marks each bullet:
  met      you clearly have it (a resume skill, your major, your class year...)
  partial  close but not exact (a related tool, "or related field")
  missing  you clearly don't (a skill not on your resume, grad students only...)
  check    needs you to confirm (citizenship, GPA, coursework)
  soft     soft skills and logistics (not counted)
"""
from __future__ import annotations

import re

from .match import CITIZEN_RX, CLEARANCE_RX, GPA_RX, GRAD_DEGREE_ONLY_RX
from .util import compile_terms, hits

REQ_HEAD = re.compile(
    r"(?i)qualifications|requirements|^(basic|minimum|required|must[- ]have|essential|skills)|"
    r"you['’]ll (need|bring)|you will (need|bring)|what we['’]?re looking for|what we are looking for|"
    r"what you bring|^you have|who you are|about you|you should have|your experience|^experience")
PREF_HEAD = re.compile(r"(?i)(preferred|desired|nice[- ]to[- ]have|bonus|plus(es)?\b|additional (skills|qualifications))")
END_HEAD = re.compile(
    r"(?i)responsibilit|what you(['’]| wi)ll (do|work on)|duties|benefits|compensation|\bpay\b|salary|about|"
    r"who we are|the role|perks|schedule|why |equal opportunity|\beeo|additional information|"
    r"security clearance|physical|work environment|overview|summary|job description|the opportunity|"
    r"life at|culture|day to day|your impact|the team|location")
# Legal boilerplate headings ("ITAR Requirements", "E-Verify Statement") are never qualifications
SKIP_HEAD = re.compile(r"(?i)itar|export|e-verify|right to work|equal opportunity|\beeo|statement|notice|"
                       r"disclaimer|privacy|accommodation|read more")
COURSE_ONLY_RX = re.compile(r"(?i)course\s*work|\bcourses\b|\bclasses\b")
BULLET = re.compile(r"^\s*(?:[-•*▪●◦]|\d+[.)])\s*")
PLUS_RX = re.compile(r"(?i)\b(is a plus|a plus|nice to have|preferred|desired|bonus)\b")

AERO_MAJOR_RX = re.compile(r"(?i)aerospace|aeronautic|astronautic|mechanical|\bstem\b|engineering discipline|"
                           r"any engineering|engineering degree|engineering (major|program|field)")
# A field word right before "engineering" means a specific discipline, not engineering in general
DISCIPLINE_RX = re.compile(r"(?i)(electrical|computer|software|systems|chemical|civil|industrial|marine|ocean|"
                           r"manufacturing|materials|biomedical|nuclear|environmental|petroleum|optical|network|"
                           r"security|robotics|mechatronics)\W*(and\W+\w+\W+)?$")
RELATED_FIELD_RX = re.compile(r"(?i)related (technical |engineering |stem |science )?(field|discipline|degree|major)|equivalent")
OTHER_FIELD_RX = re.compile(r"(?i)electrical|computer (science|engineering)|software|information|chemical|civil|"
                            r"industrial|systems engineering|physics|mathematics|business|finance|data science|cyber|"
                            r"naval|marine|ocean|supply chain|logistics|health|safety|accounting")
GRAD_LEVEL_RX = re.compile(r"(?i)(?<!under)graduate (school|study|studies|coursework|level|research)|master['’]?s|ph\.?d|doctoral")
UNDERGRAD_RX = re.compile(r"(?i)undergrad|bachelor|b\.?s\.?\b")
FOREIGN_CLEARANCE_RX = re.compile(r"(?i)australian|british|\buk\b|canadian|nz government")
STANDING_RX = re.compile(r"(?i)(freshman|sophomore|junior|senior)s?\s*(year\s*)?(standing|status|or (later|above|higher))|"
                         r"entering (your |their )?(freshman|sophomore|junior|senior)|"
                         r"(completed|finished) (your |their )?(freshman|sophomore|junior) year")
CREDITS_RX = re.compile(r"(?i)(\d{2,3})\s*(?:\+\s*)?(?:credit hours|hours of credit|credits|semester hours)")
INTEREST_RX = re.compile(r"(?i)^(a |an )?(strong |genuine |deep )?(desire|interest|passion|enthusiasm|curiosity)\b")
DEGREE_RX = re.compile(r"(?i)\b(degree|pursuing|enrolled|major(ing)?|bachelor'?s?|undergraduate|studying|"
                       r"b\.?s\.?\b|student in)")
RETURNING_RX = re.compile(r"(?i)return(ing)? to school|continu\w+ (your |their )?(education|studies|academic)")
ACTIVE_CLEARANCE_RX = re.compile(r"(?i)(active|existing|current)\b[^\n]{0,60}?clearance|clearance required on day")
COURSEWORK_RX = re.compile(r"(?i)course\s*work|courses|classes in|knowledge of|understanding of|fundamentals")
PROJECT_RX = re.compile(r"(?i)\b(club|project team|design team|student (team|project|organization)|competition|"
                        r"personal projects?|hands-on projects?|school projects?|academic projects?|team projects?)s?\b")
SOFT_RX = re.compile(r"(?i)communicat|teamwork|team player|collaborat|self[- ]starter|motivat|detail[- ]oriented|"
                     r"attention to detail|problem[- ]solv|enthusias|passion|independently|fast[- ]paced|eager|curious|"
                     r"initiative|ownership|organiz|prioritiz|first principles|mindset|work ethic|interpersonal|"
                     r"adaptab|willing(ness)? to learn|ability to learn|critical thinking")
LOGISTICS_RX = re.compile(r"(?i)available|full[- ]time|\d+\s*hours|relocat|on[- ]?site|in[- ]person|commut|"
                          r"\d+\s*weeks|lift \d+|travel|work authorization|sponsorship|drug|background check|"
                          r"located near|reliable transportation")

MAX_ITEMS = {"required": 12, "preferred": 8}


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or BULLET.match(line) or len(s) > 70 or s.endswith((".", ",", ";")):
        return False
    if s.endswith(":") or s.isupper():
        return True
    return len(s.split()) <= 6 and bool(REQ_HEAD.search(s) or PREF_HEAD.search(s) or END_HEAD.search(s)
                                        or SKIP_HEAD.search(s))


def extract(desc: str) -> dict[str, list[str]]:
    """Returns {"required": [...], "preferred": [...]} bullet texts."""
    out = {"required": [], "preferred": []}
    seen = set()
    section, had_bullets, lone_marker = None, False, False
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        if BULLET.fullmatch(line):  # "-" alone on a line: its text is on the next line
            lone_marker = True
            continue
        is_bullet = bool(BULLET.match(raw)) or lone_marker
        lone_marker = False
        if not is_bullet and _is_heading(raw):
            head = line.rstrip(":").strip()
            if SKIP_HEAD.search(head):
                section = None
            elif PREF_HEAD.search(head) and len(head.split()) <= 8:
                section = "preferred"
            elif REQ_HEAD.search(head):
                section = "required"
            else:
                section = None  # Responsibilities, Benefits, Pay, About us...
            had_bullets = False
            continue
        if section is None:
            continue
        if not is_bullet and had_bullets:
            section = None  # plain paragraph after the list: the section is over
            continue
        if not is_bullet and not 4 < len(line.split()) <= 40:
            continue  # intro paragraph before the list, or a stray "Read more"
        had_bullets = had_bullets or is_bullet
        text = BULLET.sub("", line).strip()
        if len(text) < 3 or text.lower() in seen:
            continue
        seen.add(text.lower())
        sec = "preferred" if section == "required" and PLUS_RX.search(text) else section
        if len(out[sec]) < MAX_ITEMS[sec]:
            out[sec].append(text)
    return out


def _gpa(x: float) -> str:
    s = f"{x:.2f}"
    return s[:-1] if s.endswith("0") else s


class QualChecker:
    def __init__(self, matcher, profile: dict):
        self.m = matcher
        st = profile["student"]
        self.major = st.get("major", "")
        self.gpa = float(st.get("gpa") or 0)
        self.citizen = st.get("us_citizen")  # True / False / missing = unknown
        self.gaps = [(g["name"], g["aliases"]) for g in matcher.gaps]

    def _one(self, b: str, coursework: bool) -> tuple[str, str]:
        t = b.lower()
        # ---- eligibility
        if CLEARANCE_RX.search(b) or CITIZEN_RX.search(b):
            if FOREIGN_CLEARANCE_RX.search(b):
                return "missing", "Needs a foreign government clearance"
            if ACTIVE_CLEARANCE_RX.search(b):
                return "missing", "Needs a clearance you'd already have to hold"
            if self.citizen is True:
                return "met", "You're a U.S. citizen"
            if self.citizen is False:
                return "missing", "Requires U.S. citizenship / U.S. person"
            return "check", "U.S. citizenship / U.S. person (set us_citizen in profile.toml)"
        gm = GPA_RX.search(b)
        if gm:
            need = float(gm.group(1) or gm.group(2))
            if self.gpa:
                return ("met", f"Your GPA {_gpa(self.gpa)} ≥ {_gpa(need)}") if self.gpa >= need else \
                       ("missing", f"Needs GPA {_gpa(need)}+")
            return "check", f"Needs GPA {_gpa(need)}+"
        g = self.m.grad_check(b)
        if g:
            return ("met", f"Grad date rule ({g[1]}) fits your {self.m.grad_year}") if g[0] else \
                   ("missing", f"Grad date rule ({g[1]}) doesn't fit your {self.m.grad_year}")
        sm = STANDING_RX.search(b)
        if sm:
            words = sm.group(0).lower()
            if "senior" in words:
                return "missing", "Wants senior standing (you'll be a rising junior)"
            return "met", "You'll be a rising junior"
        cm = CREDITS_RX.search(b)
        if cm:
            have = 30 * self.m.completed_years
            need = int(cm.group(1))
            return ("met", f"You'll have about {have} credits") if need <= have else \
                   ("missing", f"Needs {need} credits (you'll have about {have})")
        c = self.m.class_check(b)
        if c:
            return ("met" if c[0] == "good" else "missing"), c[1]
        if RETURNING_RX.search(b):
            return "met", f"You'll still be in school (grad {self.m.grad_year})"
        if DEGREE_RX.search(b) and not COURSE_ONLY_RX.search(b):
            if GRAD_DEGREE_ONLY_RX.search(b) or (GRAD_LEVEL_RX.search(b) and not UNDERGRAD_RX.search(b)):
                return "missing", "Asks for a graduate degree"
            if AERO_MAJOR_RX.search(b):
                return "met", f"You're {self.major}"
            # "a degree in Engineering" (any discipline) vs "Electrical Engineering"
            if any(not DISCIPLINE_RX.search(b[:m.start()]) for m in re.finditer(r"(?i)engineering", b)):
                return "met", f"Engineering degree: {self.major} counts"
            if RELATED_FIELD_RX.search(b):
                return "partial", f"Other majors listed; {self.major} may count as related"
            if OTHER_FIELD_RX.search(b):
                return "missing", "Wants a different major"
            return "met", "You're enrolled in a degree program"
        if GRAD_LEVEL_RX.search(b) and not UNDERGRAD_RX.search(b):
            return "missing", "Asks for graduate-level experience"
        # ---- skills from your resume
        have = [s["name"] for s in self.m.skills if hits(s["aliases"], t)]
        if have:
            return "met", "You have: " + ", ".join(have[:3])
        close = [(hits(s["related"], t)[0].rstrip("*"), s["name"]) for s in self.m.skills if hits(s["related"], t)]
        if close:
            tool, base = close[0]
            return "partial", f"Close: {tool.upper() if len(tool) <= 5 else tool.title()} ≈ {base}"
        if PROJECT_RX.search(b):
            return "met", "Your Formula SAE, rocketry, and rover projects"
        if INTEREST_RX.search(b):
            return "soft", ""
        missing = [name for name, al in self.gaps if hits(al, t)]
        if missing:
            if coursework or COURSEWORK_RX.search(b):
                return "check", "Coursework: " + ", ".join(missing[:2]) + " (check your classes)"
            return "missing", "Not on your resume: " + ", ".join(missing[:3])
        if coursework or COURSEWORK_RX.search(b):
            return "check", "Coursework or background (check your classes)"
        if SOFT_RX.search(b) or LOGISTICS_RX.search(b):
            return "soft", ""
        return "check", ""

    def check(self, desc: str) -> dict | None:
        found = extract(desc)
        if not found["required"] and not found["preferred"]:
            return None
        result = {}
        for sec, items in found.items():
            rows, coursework = [], False
            for b in items:
                if b.endswith(":"):  # "Coursework in:" / "Experience with:" introduces sub-bullets
                    coursework = bool(COURSEWORK_RX.search(b))
                    continue
                status, why = self._one(b, coursework)
                rows.append({"text": b[:160], "status": status, "why": why})
            result[sec] = rows
        req = [r for r in result.get("required", []) if r["status"] in ("met", "partial", "missing")]
        met = sum(1 for r in req if r["status"] == "met") + 0.5 * sum(1 for r in req if r["status"] == "partial")
        result["summary"] = {
            "required_checked": len(req),
            "required_met": met,
            "required_missing": sum(1 for r in req if r["status"] == "missing"),
            "to_check": sum(1 for sec in ("required", "preferred") for r in result.get(sec, [])
                            if r["status"] == "check" and r["why"]),
            "preferred_met": sum(1 for r in result.get("preferred", []) if r["status"] == "met"),
            "preferred_total": sum(1 for r in result.get("preferred", []) if r["status"] in ("met", "partial", "missing")),
        }
        return result

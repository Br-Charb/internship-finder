"""Job filtering, category selection, and requirement-snippet extraction.

The source adapters in :mod:`jobfinder.sources` normalize ATS-specific payloads.
This module is deliberately independent of AWS and Discord so routing can be
tested locally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_INTERNSHIP_TITLE = re.compile(
    r"(?i)(?<![a-z])(intern(?:ship)?|co[ -]?op|student(?: trainee)?|trainee|"
    r"apprentic(?:e|eship)|new[ -]?grad(?:uate)?|university[ -]?grad(?:uate)?|"
    r"early[ -]?career|summer(?:\s+[a-z&]+){0,3}\s+(?:analyst|associate))(?![a-z])"
)

_NON_TARGET_TITLE = re.compile(
    r"(?i)(?<![a-z])(sales|marketing|recruit(?:er|ing)|talent acquisition|human resources|"
    r"legal|counsel|customer success|customer support|communications?|public relations|"
    r"business development)(?![a-z])"
)


def is_target_job(title: str, scope: str = "internships", employment_type: str = "") -> bool:
    """Return whether a posting belongs in this internship-oriented feed.

    Set ``JOB_SCOPE=all`` in Lambda to route every categorized posting from the
    configured ATS boards instead.
    """
    title = title or ""
    if _NON_TARGET_TITLE.search(title):
        return False
    if scope.lower() == "all":
        return True
    return bool(
        _INTERNSHIP_TITLE.search(title)
        or re.search(r"(?i)(?<![a-z])(intern(?:ship)?|co[ -]?op|apprentice)(?![a-z])", employment_type or "")
    )


def _compile_term(term: str) -> re.Pattern[str]:
    """Compile a literal keyword with useful word boundaries."""
    escaped = re.escape(term.strip().lower()).replace(r"\ ", r"[\s-]+")
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    webhook_env: str
    color: int
    title_terms: tuple[str, ...]
    description_terms: tuple[str, ...]
    strong_title_terms: tuple[str, ...] = ()
    _title_patterns: tuple[re.Pattern[str], ...] = field(init=False, repr=False)
    _description_patterns: tuple[re.Pattern[str], ...] = field(init=False, repr=False)
    _strong_patterns: tuple[re.Pattern[str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_title_patterns", tuple(_compile_term(t) for t in self.title_terms))
        object.__setattr__(self, "_description_patterns", tuple(_compile_term(t) for t in self.description_terms))
        object.__setattr__(self, "_strong_patterns", tuple(_compile_term(t) for t in self.strong_title_terms))

    def score(self, title: str, description: str) -> tuple[int, list[str]]:
        title_hits = [term for term, rx in zip(self.title_terms, self._title_patterns) if rx.search(title)]
        desc_hits = [
            term for term, rx in zip(self.description_terms, self._description_patterns)
            if rx.search(description)
        ]
        strong_hits = [
            term for term, rx in zip(self.strong_title_terms, self._strong_patterns) if rx.search(title)
        ]
        # Titles communicate the discipline more reliably than boilerplate in a
        # long description. Strong phrases break common ties such as "systems".
        score = 6 * len(title_hits) + min(len(desc_hits), 8) + 10 * len(strong_hits)
        return score, strong_hits + title_hits + desc_hits


# Tie order is intentional: a discipline-specific engineering match beats the
# generic Finance keyword "analyst" (for example, "Propulsion Analyst Intern").
CATEGORIES: tuple[Category, ...] = (
    Category(
        key="aerospace",
        label="Aerospace Engineering",
        webhook_env="DISCORD_WEBHOOK_AEROSPACE",
        color=0x5865F2,
        title_terms=(
            "aerospace", "aerodynamics", "aeronautical", "astronautical", "avionics",
            "flight dynamics", "flight test", "gnc", "guidance navigation and control",
            "mission design", "orbital", "propulsion", "rocket", "spacecraft", "satellite",
        ),
        description_terms=(
            "aerodynamics", "aerospace engineering", "aircraft", "astrodynamics", "avionics",
            "cfd", "flight controls", "gnc", "guidance navigation and control", "launch vehicle",
            "orbital mechanics", "propulsion", "rocket", "spacecraft", "satellite",
        ),
        strong_title_terms=("aerospace engineer", "aerodynamics engineer", "propulsion engineer", "gnc engineer"),
    ),
    Category(
        key="mechanical",
        label="Mechanical Engineering",
        webhook_env="DISCORD_WEBHOOK_MECHANICAL",
        color=0xF1C40F,
        title_terms=(
            "mechanical", "mechanisms", "product design", "manufacturing", "tooling",
            "industrial engineering", "structural", "structures", "thermal", "thermodynamics",
            "fluids", "fluid systems", "stress engineer", "materials engineer", "vehicle dynamics",
            "automotive", "reliability engineer", "additive manufacturing", "composites",
        ),
        description_terms=(
            "cad", "catia", "creo", "solidworks", "siemens nx", "autocad", "fusion 360",
            "fea", "finite element", "ansys", "abaqus", "nastran", "gd&t", "tolerance stack",
            "heat transfer", "thermal analysis", "thermal management", "thermodynamics",
            "fluid mechanics", "cfd", "mechanical design", "mechanical engineering",
            "structural analysis", "stress analysis", "manufacturing engineering", "dfm",
            "design for manufacturing", "machining", "cnc", "injection molding", "sheet metal",
            "additive manufacturing", "3d printing", "composites", "materials science",
            "prototyping", "mechanisms", "plm", "windchill",
        ),
        strong_title_terms=(
            "mechanical engineer", "mechanical design engineer", "thermal engineer",
            "structures engineer", "structural engineer", "manufacturing engineer",
            "product design engineer", "stress engineer", "fluids engineer",
        ),
    ),
    Category(
        key="hardware",
        label="Hardware / Embedded / Computer Engineering",
        webhook_env="DISCORD_WEBHOOK_HARDWARE",
        color=0xE67E22,
        title_terms=(
            "computer engineering", "electrical", "electronics", "embedded", "firmware", "fpga",
            "hardware", "pcb", "board design", "power electronics", "power systems", "rf",
            "radio frequency", "antenna", "robotics", "mechatronics", "controls engineer",
            "systems engineer", "asic", "silicon", "semiconductor", "digital design",
            "analog design", "mixed signal", "chip design", "soc", "rtl", "verification engineer",
            "signal integrity", "test hardware", "electromechanical",
        ),
        description_terms=(
            "circuit design", "computer engineering", "electrical engineering", "embedded systems",
            "firmware", "fpga", "asic", "microcontroller", "microprocessor", "bare metal", "rtos",
            "arm cortex", "embedded c", "device driver", "bootloader", "spi", "i2c", "uart",
            "can bus", "pcb", "printed circuit board", "schematic capture", "board bring-up",
            "altium", "cadence", "orcad", "signal integrity", "power integrity", "oscilloscope",
            "logic analyzer", "digital electronics", "analog circuits", "mixed signal",
            "power electronics", "motor control", "control systems", "sensors", "actuators",
            "signal processing", "rf design", "wireless", "antenna", "emi", "emc",
            "systems engineering", "verilog", "systemverilog", "vhdl", "rtl", "soc",
            "semiconductor", "silicon validation", "hardware validation",
        ),
        strong_title_terms=(
            "electrical engineer", "electronics engineer", "computer engineer", "embedded engineer",
            "firmware engineer", "fpga engineer", "hardware engineer", "asic engineer",
            "pcb engineer", "rf engineer", "digital design engineer", "analog design engineer",
        ),
    ),
    Category(
        key="software",
        label="Software Engineering",
        webhook_env="DISCORD_WEBHOOK_SOFTWARE",
        color=0x2ECC71,
        title_terms=(
            "software", "frontend", "front end", "backend", "back end", "full stack", "fullstack",
            "web developer", "mobile developer", "ios", "android", "devops", "site reliability",
            "machine learning", "data engineer", "cloud engineer", "swe",
        ),
        description_terms=(
            "python", "java", "c++", "c#", "javascript", "typescript", "react", "node.js",
            "software engineering", "api", "sql", "aws", "cloud computing", "kubernetes",
            "docker", "git", "linux", "data structures", "algorithms",
        ),
        strong_title_terms=("software engineer", "frontend engineer", "backend engineer", "full stack engineer"),
    ),
    Category(
        key="finance",
        label="Finance / Info Systems / FinTech",
        webhook_env="DISCORD_WEBHOOK_FINANCE",
        color=0x9B59B6,
        title_terms=(
            "finance", "financial", "fp&a", "fintech", "analyst", "accounting", "audit",
            "treasury", "tax", "actuarial", "underwriting", "credit", "banking",
            "investment banking", "capital markets", "asset management", "wealth management",
            "portfolio", "private equity", "venture capital", "quantitative", "quant",
            "trading", "trader", "payments", "payment operations", "fraud", "risk analyst",
            "business analyst", "operations analyst", "business systems", "information systems",
            "management information systems", "information technology", "business intelligence",
            "enterprise systems", "erp", "cybersecurity", "it audit", "it",
        ),
        description_terms=(
            "financial analysis", "financial modeling", "fp&a", "fintech", "accounting",
            "audit", "treasury", "corporate finance", "investment banking", "capital markets",
            "asset management", "wealth management", "portfolio management", "private equity",
            "venture capital", "equity research", "fixed income", "securities", "derivatives",
            "quantitative finance", "algorithmic trading", "financial markets", "risk management",
            "credit risk", "market risk", "operational risk", "underwriting", "actuarial",
            "payments", "payment processing", "banking", "fraud detection", "financial technology",
            "information systems", "management information systems", "information technology",
            "business systems", "enterprise systems", "business intelligence", "erp", "sap",
            "oracle financials", "data analytics", "power bi", "tableau", "excel", "vba",
        ),
        strong_title_terms=(
            "financial analyst", "finance analyst", "fp&a", "quantitative analyst",
            "quantitative researcher", "quantitative trader", "investment banking",
            "asset management", "risk analyst", "actuarial analyst", "information systems",
            "business systems analyst", "fintech", "payments analyst",
        ),
    ),
)

CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}


def categorize_job(title: str, description: str) -> tuple[Category | None, list[str]]:
    """Choose one primary category and return its matching keywords.

    A title keyword is sufficient. If the title is generic, at least two distinct
    description keywords are required, which avoids classifying every engineering
    role as software merely because its boilerplate mentions Python once.
    """
    title = (title or "").lower()
    description = (description or "").lower()
    ranked: list[tuple[int, int, Category, list[str]]] = []
    for priority, category in enumerate(CATEGORIES):
        score, hits = category.score(title, description)
        title_match = any(rx.search(title) for rx in category._title_patterns + category._strong_patterns)
        description_hit_count = sum(bool(rx.search(description)) for rx in category._description_patterns)
        if title_match or description_hit_count >= 2:
            ranked.append((score, -priority, category, hits))
    if not ranked:
        return None, []
    _, _, category, hits = max(ranked, key=lambda row: (row[0], row[1]))
    return category, list(dict.fromkeys(hits))


_REQUIREMENTS_HEADING = re.compile(
    r"(?i)^(minimum |basic |preferred )?(requirements?|qualifications?|what you(?:'|’)ll need|"
    r"what we(?:'|’)re looking for|you have|skills)(?:\s+and\s+experience)?\s*:?$"
)
_OTHER_HEADING = re.compile(
    r"(?i)^(responsibilities|what you(?:'|’)ll do|about|benefits|compensation|salary|"
    r"equal opportunity|physical demands|working conditions)\s*:?$"
)
_BULLET = re.compile(r"^\s*(?:[-*•▪◦‣]|\d+[.)])\s*")


def requirements_snippet(description: str, limit: int = 750) -> str:
    """Extract a short, Discord-safe requirements excerpt from plain job text."""
    if not description:
        return "Requirements were not included by this source. Open the posting for details."

    lines = [re.sub(r"\s+", " ", line).strip() for line in description.splitlines()]
    start = next((i + 1 for i, line in enumerate(lines) if _REQUIREMENTS_HEADING.match(line)), None)
    selected: list[str] = []
    if start is not None:
        for line in lines[start:]:
            if not line:
                continue
            if _OTHER_HEADING.match(line) or (_REQUIREMENTS_HEADING.match(line) and selected):
                break
            clean = _BULLET.sub("", line).strip()
            if 8 <= len(clean) <= 350:
                selected.append(clean)
            if len(selected) == 3:
                break

    if not selected:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", description)
        signal = re.compile(r"(?i)\b(required|qualification|experience|proficien|degree|knowledge of)\b")
        selected = [re.sub(r"\s+", " ", sentence).strip() for sentence in sentences if signal.search(sentence)][:2]

    if not selected:
        selected = [re.sub(r"\s+", " ", description).strip()]

    snippet = "\n".join(f"• {line}" for line in selected if line)
    if len(snippet) <= limit:
        return snippet
    return snippet[: limit - 1].rsplit(" ", 1)[0] + "…"


_ACADEMIC_DATE_TEXT = (
    r"(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?|spring|summer|fall|autumn|winter)\s+20\d{2}|20\d{2})"
)
_ACADEMIC_DATE = re.compile(_ACADEMIC_DATE_TEXT, re.IGNORECASE)
_GRADUATION_TRIGGER = re.compile(
    r"(?i)(?:graduat(?:e|es|ed|ing|ion)|class\s+of|"
    r"(?:expected|anticipated)\s+(?:degree\s+)?completion|"
    r"degree\s+(?:completion|conferral)\s+date)"
)
_MONTHS = {
    "january": "Jan", "jan": "Jan", "february": "Feb", "feb": "Feb",
    "march": "Mar", "mar": "Mar", "april": "Apr", "apr": "Apr",
    "may": "May", "june": "Jun", "jun": "Jun", "july": "Jul", "jul": "Jul",
    "august": "Aug", "aug": "Aug", "september": "Sep", "sept": "Sep", "sep": "Sep",
    "october": "Oct", "oct": "Oct", "november": "Nov", "nov": "Nov",
    "december": "Dec", "dec": "Dec",
}


def _normalize_academic_date(value: str) -> str:
    parts = value.strip().split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0].lower()
    label = _MONTHS.get(first, first.title())
    if label == "Autumn":
        label = "Fall"
    return f"{label} {parts[-1]}"


def graduation_window(description: str) -> str:
    """Extract an explicitly stated graduation date/range, otherwise ``?``.

    Only sentences or lines that mention graduation, class year, or degree
    completion are considered. This avoids mistaking the internship dates or a
    copyright year for the student's graduation window.
    """
    if not description:
        return "?"

    chunks = re.split(r"\n+|(?<=[.!?])\s+", description)
    for raw_chunk in chunks:
        chunk = re.sub(r"\s+", " ", raw_chunk).strip()
        if not chunk or not _GRADUATION_TRIGGER.search(chunk):
            continue
        dates = list(dict.fromkeys(
            _normalize_academic_date(match.group(0))
            for match in _ACADEMIC_DATE.finditer(chunk)
        ))
        if not dates:
            continue
        if len(dates) >= 2:
            return f"{dates[0]} – {dates[1]}"

        lowered = chunk.lower()
        value = dates[0]
        if re.search(r"\b(?:or|and)\s+(?:later|after|beyond)\b|\bor\s+later\b", lowered):
            return f"{value} or later"
        if re.search(r"\b(?:or|and)\s+(?:earlier|before)\b|\bor\s+earlier\b", lowered):
            return f"{value} or earlier"
        return value
    return "?"

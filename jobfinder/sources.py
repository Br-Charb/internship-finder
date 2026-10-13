"""Fetch internship postings from every source listed in sources.toml.

Each fetcher returns a list of job dicts with these keys:
  company, title, location, url, source, posted (YYYY-MM-DD or ""),
  description (plain text, may be ""), season_hint (str, may be "")
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

from .util import FetchError, get_json, html_to_text, parse_ats_url, post_json, request, strip_tracking


def _date_from_iso(s) -> str:
    if not s:
        return ""
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(s)[:10]


def _date_from_epoch(n, ms=False) -> str:
    try:
        n = float(n) / (1000 if ms else 1)
        return dt.datetime.fromtimestamp(n, dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


# ------------------------------------------------------------ GitHub lists

def fetch_simplify_list(src: dict) -> list[dict]:
    data, err = None, None
    for url in src["urls"]:
        try:
            data = get_json(url, timeout=60)
            break
        except (FetchError, ValueError) as e:
            err = e
    if data is None:
        raise FetchError(f"list not reachable ({err})")
    jobs = []
    for item in data:
        if not item.get("active", True) or not item.get("is_visible", True):
            continue
        terms = item.get("terms") or []
        season = ", ".join(terms) if isinstance(terms, list) else str(terms)
        locs = item.get("locations") or []
        jobs.append({
            "company": item.get("company_name", "").strip(),
            "title": item.get("title", "").strip(),
            "location": "; ".join(locs) if isinstance(locs, list) else str(locs),
            "url": strip_tracking(item.get("url", "")),
            "source": src["name"],
            "posted": _date_from_epoch(item.get("date_posted")),
            "description": "",
            "season_hint": season,
            "sponsorship": item.get("sponsorship", ""),
        })
    return jobs


def github_list_revision(src: dict) -> str:
    """Return the newest commit SHA for a raw GitHub list, or ``""``.

    Lambda stores this value in DynamoDB. If it has not changed, the large JSON
    listing does not need to be downloaded and parsed again. Failure to check a
    revision is deliberately non-fatal: the caller simply fetches the list.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for raw_url in src.get("urls", []):
        parsed = urlparse(raw_url)
        if parsed.hostname != "raw.githubusercontent.com":
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 4:
            continue
        owner, repo, branch = parts[:3]
        try:
            commit = get_json(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}",
                headers=headers,
                timeout=20,
                retries=1,
            )
            sha = str(commit.get("sha") or "").strip()
            if sha:
                return sha
        except (FetchError, ValueError):
            continue
    return ""


def fetch_getro(src: dict) -> list[dict]:
    """Getro-powered job boards (e.g. the Space Capital board). Each posting links
    to the company's own hiring page, so descriptions are filled in later."""
    api = f"https://api.getro.com/api/v2/collections/{src['collection']}/search/jobs"
    found: dict = {}
    # Interns tagged as such, plus "Summer 2027" titles the board left untagged
    for body in ({"query": "", "filters": {"seniority": ["internship"]}},
                 {"query": "summer 2027 intern", "filters": {}}):
        page = 0
        while page < 150:
            res = post_json(api, {"hitsPerPage": 20, "page": page, **body}, timeout=40).get("results") or {}
            batch = res.get("jobs") or []
            for j in batch:
                found[j.get("id")] = j
            page += 1
            if not batch or page * 20 >= int(res.get("count") or 0):
                break
    jobs = []
    for j in found.values():
        jobs.append({
            "company": ((j.get("organization") or {}).get("name") or "").strip(),
            "title": (j.get("title") or "").strip(),
            "location": "; ".join(j.get("locations") or []),
            "url": strip_tracking(j.get("url", "")),
            "source": src["name"],
            "posted": _date_from_epoch(j.get("created_at")),
            "description": "",
            "season_hint": "",
        })
    return jobs


# ------------------------------------------------------------ company boards

def fetch_greenhouse(board: str, company: str) -> list[dict]:
    # Keep the five-minute listing pass small. Full descriptions are requested
    # later, and only for previously unseen candidates.
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", timeout=60)
    out = []
    for j in data.get("jobs", []):
        out.append({
            "company": company,
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "source": f"{company} careers",
            "posted": _date_from_iso(j.get("first_published") or j.get("updated_at")),
            "description": "",
            "season_hint": "",
        })
    return out


def _lever_text(j: dict) -> str:
    parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
    for lst in j.get("lists") or []:
        parts.append(lst.get("text", ""))
        parts.append(html_to_text(lst.get("content")))
    parts.append(j.get("additionalPlain") or "")
    return "\n".join(p for p in parts if p)


def fetch_lever(board: str, company: str) -> list[dict]:
    data = get_json(f"https://api.lever.co/v0/postings/{board}?mode=json", timeout=60)
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "company": company,
            "title": (j.get("text") or "").strip(),
            "location": cats.get("location") or ", ".join(cats.get("allLocations") or []),
            "url": j.get("hostedUrl") or j.get("applyUrl", ""),
            "source": f"{company} careers",
            "posted": _date_from_epoch(j.get("createdAt"), ms=True),
            "description": _lever_text(j),
            "season_hint": "",
            "employment_type": cats.get("commitment", ""),
        })
    return out


_ashby_cache: dict[str, list] = {}


def _ashby_board(board: str) -> list:
    if board not in _ashby_cache:
        data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=60)
        _ashby_cache[board] = data.get("jobs", [])
    return _ashby_cache[board]


def fetch_ashby(board: str, company: str) -> list[dict]:
    out = []
    for j in _ashby_board(board):
        if j.get("isListed") is False:
            continue
        out.append({
            "company": company,
            "title": (j.get("title") or "").strip(),
            "location": j.get("location", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "source": f"{company} careers",
            "posted": _date_from_iso(j.get("publishedAt")),
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            "season_hint": "",
            "employment_type": j.get("employmentType", ""),
        })
    return out


def fetch_smartrecruiters(board: str, company: str) -> list[dict]:
    """Fetch the public SmartRecruiters listing API without job descriptions."""
    out, offset, limit = [], 0, 100
    while True:
        data = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
            f"?limit={limit}&offset={offset}",
            timeout=60,
        )
        batch = data.get("content") or []
        for j in batch:
            location = j.get("location") or {}
            identifier = str((j.get("company") or {}).get("identifier") or board)
            title_slug = re.sub(r"[^a-z0-9]+", "-", (j.get("name") or "").lower()).strip("-")
            out.append({
                "company": company,
                "title": (j.get("name") or "").strip(),
                "location": location.get("fullLocation") or ", ".join(filter(None, [
                    location.get("city"), location.get("region"), location.get("country"),
                ])),
                "url": f"https://jobs.smartrecruiters.com/{identifier}/{j.get('id')}-{title_slug}",
                "source": f"{company} careers",
                "posted": _date_from_iso(j.get("releasedDate")),
                "description": "",
                "season_hint": "",
                "employment_type": (j.get("typeOfEmployment") or {}).get("label", ""),
            })
        offset += len(batch)
        if not batch or offset >= int(data.get("totalFound") or 0):
            return out


def fetch_rippling(board: str, company: str) -> list[dict]:
    """Fetch a Rippling ATS board and merge duplicate per-location rows."""
    data = get_json(f"https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs", timeout=60)
    found: dict[str, dict] = {}
    for j in data if isinstance(data, list) else []:
        job_id = str(j.get("uuid") or "")
        if not job_id:
            continue
        location = str((j.get("workLocation") or {}).get("label") or "").strip()
        if job_id in found:
            old_locations = [part.strip() for part in found[job_id]["location"].split(";") if part.strip()]
            if location and location not in old_locations:
                found[job_id]["location"] = "; ".join(old_locations + [location])
            continue
        found[job_id] = {
            "company": company,
            "title": (j.get("name") or "").strip(),
            "location": location,
            "url": j.get("url") or f"https://ats.rippling.com/{board}/jobs/{job_id}",
            "source": f"{company} careers",
            "posted": "",
            "description": "",
            "season_hint": "",
        }
    return list(found.values())


_INTERNISH = re.compile(r"(?i)(?<![a-z])(intern|internship|co-?op|co op|student)(?![a-z])")


def _workday_detail(host: str, tenant: str, site: str, path: str) -> dict:
    data = get_json(f"https://{host}/wday/cxs/{tenant}/{site}{path}", timeout=30)
    return data.get("jobPostingInfo") or {}


def fetch_workday(host: str, site: str, company: str, max_pages: int = 10) -> list[dict]:
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    postings, seen_paths = [], set()
    for term in ("intern", "internship", "co-op"):
        offset = 0
        for _ in range(max_pages):
            page = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                                   "searchText": term}, timeout=40)
            batch = page.get("jobPostings") or []
            for p in batch:
                if p.get("externalPath") not in seen_paths:
                    seen_paths.add(p.get("externalPath"))
                    postings.append(p)
            offset += len(batch)
            if not batch or offset >= int(page.get("total") or 0):
                break
    # Workday's list has no descriptions, so only open internship-looking ones.
    wanted = [p for p in postings if _INTERNISH.search(p.get("title", ""))]

    def detail(p):
        path = p.get("externalPath", "")
        try:
            info = _workday_detail(host, tenant, site, path)
        except FetchError:
            info = {}
        return {
            "company": company,
            "title": (p.get("title") or "").strip(),
            "location": p.get("locationsText") or info.get("location", ""),
            "url": info.get("externalUrl") or f"https://{host}/{site}{path}",
            "source": f"{company} careers",
            "posted": _date_from_iso(info.get("startDate")),
            "description": html_to_text(info.get("jobDescription")),
            "season_hint": "",
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(detail, wanted))


def fetch_eightfold(
    host: str,
    domain: str,
    company: str,
    query: str = "intern",
    max_pages: int = 100,
) -> list[dict]:
    """Fetch either generation of Eightfold's public career-site API.

    Most tenants use ``apply/v2``. A few older/locked-down sites (including the
    existing Lockheed source) still expose ``pcsx`` instead, so the latter is a
    fallback rather than a separate adapter.
    """
    apply_api = f"https://{host}/api/apply/v2/jobs"
    postings, seen = [], set()
    try:
        start = 0
        for _ in range(max_pages):
            encoded_domain = quote(domain)
            encoded_query = quote(query)
            page = get_json(
                f"{apply_api}?domain={encoded_domain}&query={encoded_query}&num=10&start={start}",
                timeout=40,
            )
            batch = page.get("positions") or []
            for p in batch:
                if p.get("id") not in seen:
                    seen.add(p.get("id"))
                    postings.append(p)
            start += len(batch)
            if not batch or start >= int(page.get("count") or 0):
                break
    except FetchError:
        pcsx_api = f"https://{host}/api/pcsx"
        postings.clear()
        seen.clear()
        for term in (query or "intern", "co-op"):
            start = 0
            for _ in range(min(max_pages, 30)):
                encoded_domain = quote(domain)
                encoded_term = quote(term)
                page = get_json(
                    f"{pcsx_api}/search?domain={encoded_domain}&query={encoded_term}&start={start}",
                    timeout=40,
                )
                page = page.get("data", page)
                batch = page.get("positions") or []
                for p in batch:
                    if p.get("id") not in seen:
                        seen.add(p.get("id"))
                        postings.append(p)
                start += len(batch)
                if not batch or start >= int(page.get("count") or 0):
                    break

    wanted = [p for p in postings if _INTERNISH.search(str(p.get("name") or ""))]

    def detail(p):
        return {
            "company": company,
            "title": (p.get("name") or "").strip(),
            "location": "; ".join(p.get("locations") or []) or str(p.get("location") or ""),
            "url": p.get("canonicalPositionUrl") or f"https://{host}{p.get('positionUrl', '')}",
            "source": f"{company} careers",
            "posted": _date_from_epoch(p.get("postedTs") or p.get("t_create")),
            "description": html_to_text(p.get("job_description")),
            "season_hint": "",
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(detail, wanted))


def fetch_clearcompany(site_id: str, company: str) -> list[dict]:
    """ClearCompany career sites. site_id is the GUID in the page's
    careers-api.clearcompany.com/v1/<site_id> request."""
    out, page_index = [], 0
    while True:
        data = get_json(f"https://careers-api.clearcompany.com/v1/{site_id}"
                        f"?pageIndex={page_index}&pageSize=100&source=CJB-0", timeout=60)
        batch = data.get("results") or []
        for j in batch:
            out.append({
                "company": company,
                "title": (j.get("positionTitle") or "").strip(),
                "location": j.get("location", ""),
                "url": re.sub(r"/apply(\?.*)?$", "", j.get("applyLink", "")),
                "source": f"{company} careers",
                "posted": _date_from_iso(j.get("postedDate") or j.get("openDate")),
                "description": html_to_text(j.get("description")),
                "season_hint": "",
            })
        page_index += 1
        if not batch or len(out) >= int(data.get("totalCount") or 0):
            return out


# ------------------------------------------------------------ USAJOBS

def fetch_usajobs(cfg: dict) -> list[dict]:
    key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key or not email:
        raise FetchError("skipped: add USAJOBS_API_KEY and USAJOBS_EMAIL secrets to enable")
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    seen, out = set(), []
    for kw in cfg.get("keywords", []):
        url = ("https://data.usajobs.gov/api/search?ResultsPerPage=250&HiringPath=student&Keyword="
               + kw.replace(" ", "%20"))
        data = get_json(url, headers=headers, timeout=60)
        for item in (data.get("SearchResult") or {}).get("SearchResultItems", []):
            d = item.get("MatchedObjectDescriptor") or {}
            pid = d.get("PositionID") or d.get("PositionURI")
            if pid in seen:
                continue
            seen.add(pid)
            details = (d.get("UserArea") or {}).get("Details") or {}
            desc = "\n".join(filter(None, [
                details.get("JobSummary", ""), d.get("QualificationSummary", ""),
                "\n".join(details.get("MajorDuties") or []),
                "U.S. citizenship is required for federal positions.",
            ]))
            apply = (d.get("ApplyURI") or [d.get("PositionURI", "")])[0]
            out.append({
                "company": " / ".join(filter(None, [d.get("OrganizationName"), d.get("DepartmentName")])),
                "title": d.get("PositionTitle", ""),
                "location": d.get("PositionLocationDisplay", ""),
                "url": apply,
                "source": "USAJOBS",
                "posted": _date_from_iso(d.get("PublicationStartDate")),
                "description": desc,
                "season_hint": "",
            })
    return out


# ------------------------------------------------------------ run them all

def fetch_all(
    cfg: dict,
    tiers: set[int] | None = None,
    only: set[str] | None = None,
    schedule_slot: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return ``(jobs, source_status)`` without letting one source stop a run.

    ``tier`` defaults to 1. The Lambda runner uses tier 1 every five minutes,
    tier 2 every fifteen minutes, and tier 3 hourly. Lower tiers are assigned a
    stable slot so a large catalog is spread across invocations. ``only`` is
    used by verification and batch seeding and bypasses slot staggering.
    """
    selected_names = {name.casefold() for name in (only or set())}
    tasks = []  # (label, kind, tier, host-group, callable)

    def add(entry: dict, kind: str, host: str, fn) -> None:
        label = str(entry["name"])
        tier = max(1, min(3, int(entry.get("tier", 1))))
        if tiers is not None and tier not in tiers:
            return
        if selected_names and label.casefold() not in selected_names:
            return
        if schedule_slot is not None and not selected_names:
            cadence = {1: 1, 2: 3, 3: 12}[tier]
            digest = hashlib.sha256(label.casefold().encode("utf-8")).digest()
            assigned_slot = int.from_bytes(digest[:4], "big") % cadence
            if schedule_slot % cadence != assigned_slot:
                return
        tasks.append((label, kind, tier, str(entry.get("group", "other")), host, fn))

    for src in cfg.get("list", []):
        add(src, "Internship list", "raw.githubusercontent.com", lambda s=src: fetch_simplify_list(s))
    for src in cfg.get("getro", []):
        add(src, "Job board", "api.getro.com", lambda s=src: fetch_getro(s))
    for c in cfg.get("greenhouse", []):
        add(c, "Greenhouse", "boards-api.greenhouse.io", lambda c=c: fetch_greenhouse(c["board"], c["name"]))
    for c in cfg.get("lever", []):
        add(c, "Lever", "api.lever.co", lambda c=c: fetch_lever(c["board"], c["name"]))
    for c in cfg.get("ashby", []):
        add(c, "Ashby", "api.ashbyhq.com", lambda c=c: fetch_ashby(c["board"], c["name"]))
    for c in cfg.get("smartrecruiters", []):
        add(c, "SmartRecruiters", "api.smartrecruiters.com",
            lambda c=c: fetch_smartrecruiters(c["board"], c["name"]))
    for c in cfg.get("rippling", []):
        add(c, "Rippling", "api.rippling.com", lambda c=c: fetch_rippling(c["board"], c["name"]))
    for c in cfg.get("workday", []):
        add(c, "Workday", c["host"], lambda c=c: fetch_workday(c["host"], c["site"], c["name"]))
    for c in cfg.get("eightfold", []):
        add(c, "Eightfold", c["host"], lambda c=c: fetch_eightfold(
            c["host"], c["domain"], c["name"], str(c.get("query", "intern"))
        ))
    for c in cfg.get("clearcompany", []):
        add(c, "ClearCompany", "careers-api.clearcompany.com",
            lambda c=c: fetch_clearcompany(c["site_id"], c["name"]))
    if "usajobs" in cfg:
        federal = {"name": "USAJOBS", **cfg["usajobs"]}
        add(federal, "Federal", "data.usajobs.gov", lambda: fetch_usajobs(cfg["usajobs"]))

    # Do not stampede a shared ATS with every company board simultaneously.
    # The short jitter also keeps scheduled invocations from always arriving on
    # the exact same millisecond as other public clients.
    host_limits: dict[str, threading.BoundedSemaphore] = {}
    for _, _, _, _, host, _ in tasks:
        host_limits.setdefault(host, threading.BoundedSemaphore(2))

    def run(task):
        label, kind, tier, group, host, fn = task
        try:
            with host_limits[host]:
                time.sleep(random.uniform(0.02, 0.18))
                jobs = fn()
            for j in jobs:
                j["source_id"] = label
            return jobs, {
                "name": label, "kind": kind, "tier": tier, "group": group,
                "ok": True, "count": len(jobs), "error": "",
            }
        except Exception as e:  # noqa: BLE001 - report anything, keep going
            return [], {
                "name": label, "kind": kind, "tier": tier, "group": group,
                "ok": False, "count": 0, "error": str(e)[:160],
            }

    jobs, status = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for got, st in pool.map(run, tasks):
            jobs.extend(got)
            status.append(st)
            mark = "ok " if st["ok"] else "ERR"
            print(f"  [{mark}] {st['name']:<45} {st['count']:>5} jobs  {st['error']}")
    return jobs, status


# ------------------------------------------------------------ fill in descriptions

def fetch_description(url: str) -> str:
    """Get the full posting text for a link from a list (lists only give titles)."""
    info = parse_ats_url(url)
    if not info:
        return ""
    if info["ats"] == "greenhouse":
        j = get_json(f"https://boards-api.greenhouse.io/v1/boards/{info['board']}/jobs/{info['id']}", timeout=30)
        return html_to_text(j.get("content"))
    if info["ats"] == "lever":
        j = get_json(f"https://api.lever.co/v0/postings/{info['board']}/{info['id']}?mode=json", timeout=30)
        return _lever_text(j)
    if info["ats"] == "ashby":
        for j in _ashby_board(info["board"]):
            if j.get("id") == info["id"]:
                return j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
        return ""
    if info["ats"] == "workday":
        d = _workday_detail(info["host"], info["tenant"], info["site"], info["path"])
        return html_to_text(d.get("jobDescription"))
    if info["ats"] == "smartrecruiters":
        j = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{info['board']}/postings/{info['id']}",
            timeout=30,
        )
        sections = ((j.get("jobAd") or {}).get("sections") or {}).values()
        return "\n".join(
            filter(None, (html_to_text(section.get("text")) for section in sections if isinstance(section, dict)))
        )
    if info["ats"] == "rippling":
        page = request(url, timeout=30)
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            page,
            re.DOTALL,
        )
        if not match:
            return ""
        data = json.loads(match.group(1))
        description = (
            (((data.get("props") or {}).get("pageProps") or {}).get("apiData") or {})
            .get("jobPost", {}).get("description", {})
        )
        if isinstance(description, dict):
            return "\n".join(html_to_text(str(value)) for value in description.values() if value)
    return ""

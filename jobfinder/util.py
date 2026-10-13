"""Small helpers: HTTP requests, HTML-to-text, keyword matching, and URL keys."""
from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.error
import urllib.request

USER_AGENT = "internship-finder-lambda/2.0 (personal internship search)"


class FetchError(Exception):
    pass


def request(url: str, method: str = "GET", data=None, headers: dict | None = None,
            timeout: int = 25, retries: int = 2) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        if isinstance(data, bytes):
            body = data
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
    last: Exception = FetchError("unknown error")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = FetchError(f"HTTP {e.code}")
            if e.code == 429 and attempt < retries:
                try:
                    retry_after = float(e.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after = 5.0 * (attempt + 1)
                time.sleep(min(max(retry_after, 1.0), 30.0))
                continue
            if e.code in (400, 401, 403, 404, 405, 410, 422):
                break  # retrying won't help
        except Exception as e:  # timeouts, DNS, bad SSL...
            last = FetchError(str(e) or e.__class__.__name__)
        time.sleep(1.5 * (attempt + 1))
    raise last


def get_json(url: str, **kw):
    return json.loads(request(url, **kw))


def post_json(url: str, data, **kw):
    return json.loads(request(url, method="POST", data=data, **kw))


# ---------------------------------------------------------------- text

_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/li|/h[1-6]|/div|/tr|/ul|/ol)\s*/?\s*>")
_LI = re.compile(r"(?i)<\s*li[^>]*>")
_TAG = re.compile(r"<[^>]+>")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its HTML
    s = _BLOCK_TAGS.sub("\n", s)
    s = _LI.sub("\n- ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


def compile_terms(terms) -> list[tuple[str, re.Pattern]]:
    """Turn ["rocket*", "c++", "cad"] into whole-word regexes.

    A trailing * means "word starts with". Matching is case-insensitive.
    """
    out = []
    for t in terms:
        t = t.strip().lower()
        if not t:
            continue
        prefix = t.endswith("*")
        core = t[:-1] if prefix else t
        pat = r"(?<![a-z0-9])" + re.escape(core) + ("" if prefix else r"(?![a-z0-9])")
        out.append((t, re.compile(pat)))
    return out


def hits(compiled, text: str) -> list[str]:
    return [t for t, rx in compiled if rx.search(text)]


# ---------------------------------------------------------------- URLs

_GH_PATH = re.compile(r"greenhouse\.io/([\w-]+)/jobs/(\d+)")
_GH_EMBED = re.compile(r"greenhouse\.io/embed/job_app\?(?:.*&)?for=([\w-]+).*?token=(\d+)")
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")
_LEVER = re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})")
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})")
_SMART_API = re.compile(r"api\.smartrecruiters\.com/v1/companies/([^/?#]+)/postings/([^/?#]+)")
_SMART_JOBS = re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/([^/?#]+)")
_RIPPLING = re.compile(r"ats\.rippling\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?([^/?#]+)/jobs/([0-9a-f-]{36})")
_WORKDAY = re.compile(
    r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)(/job/[^?#]+)")


def parse_ats_url(url: str) -> dict | None:
    """Recognise links on common hiring systems so we can fetch the full posting."""
    if not url:
        return None
    m = _GH_PATH.search(url) or _GH_EMBED.search(url)
    if m:
        return {"ats": "greenhouse", "board": m.group(1), "id": m.group(2)}
    m = _LEVER.search(url)
    if m:
        return {"ats": "lever", "board": m.group(1), "id": m.group(2)}
    m = _ASHBY.search(url)
    if m:
        return {"ats": "ashby", "board": m.group(1), "id": m.group(2)}
    m = _SMART_API.search(url) or _SMART_JOBS.search(url)
    if m:
        job_id = m.group(2).split("-", 1)[0] if m.re is _SMART_JOBS else m.group(2)
        return {"ats": "smartrecruiters", "board": m.group(1), "id": job_id}
    m = _RIPPLING.search(url)
    if m:
        return {"ats": "rippling", "board": m.group(1), "id": m.group(2)}
    m = _WORKDAY.search(url)
    if m:
        tenant, wd, site, path = m.groups()
        return {"ats": "workday", "host": f"{tenant}.{wd}.myworkdayjobs.com",
                "tenant": tenant, "site": site, "path": path}
    return None


def job_key(
    url: str,
    company: str,
    title: str,
    source: str = "",
    source_job_id: str = "",
) -> str:
    """A stable ID so the same job from two sources is only shown once."""
    info = parse_ats_url(url or "")
    if info:
        if info["ats"] == "workday":
            last = info["path"].rstrip("/").rsplit("/", 1)[-1]
            req = re.search(r"_([A-Za-z]*-?\d{4,}(?:-\d+)?)$", last)
            return f"wd:{info['tenant']}:{(req.group(1) if req else last).lower()}"
        return f"{info['ats'][:2]}:{info['id']}"
    m = _GH_JID.search(url or "")
    if m:
        return f"gr:{m.group(1)}"
    if source_job_id:
        source_name = re.sub(r"[^a-z0-9]+", "-", (source or company).casefold()).strip("-")
        return f"src:{source_name}:{str(source_job_id).casefold()}"
    clean = re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")
    clean = re.sub(r"/apply$", "", clean)
    if clean:
        return "url:" + clean
    return "t:" + re.sub(r"\W+", "-", f"{company}-{title}".lower())


def strip_tracking(url: str) -> str:
    """Remove utm_/ref tracking parameters but keep ones the site needs."""
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    keep = [p for p in query.split("&")
            if p and not re.match(r"(?i)(utm_[a-z]+|ref|source|src)=", p)]
    return base + ("?" + "&".join(keep) if keep else "")

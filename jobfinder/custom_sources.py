"""Adapters for company-specific public career sites.

These sites do not expose one of the reusable ATS APIs in ``sources.py``.
Every function returns the same normalized dictionaries as the generic
adapters and intentionally fails loudly when an undocumented page shape moves.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from urllib.parse import urlencode, urljoin

from .util import FetchError, html_to_text, post_json, request


_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _iso(value) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10]


def _epoch(value) -> str:
    try:
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _job(company: str, title: str, location: str, url: str, *, posted: str = "",
         country: str = "", description: str = "", employment_type: str = "",
         source_job_id: str = "", modality: str = "") -> dict:
    return {
        "company": company,
        "title": (title or "").strip(),
        "location": location or "",
        "country": country or "",
        "url": url or "",
        "source": f"{company} careers",
        "posted": posted or "",
        "description": description or "",
        "employment_type": employment_type or "",
        "source_job_id": source_job_id or "",
        "modality": modality or "",
        "season_hint": "",
    }


def fetch_apple(company: str, max_pages: int = 30) -> list[dict]:
    marker = "window.__staticRouterHydrationData = JSON.parse("
    out: list[dict] = []
    for page_number in range(1, max_pages + 1):
        url = (
            "https://jobs.apple.com/en-us/search?"
            f"team=internships-STDNT-INTRN&page={page_number}"
        )
        body = request(url, headers=_BROWSER_HEADERS, timeout=60)
        start = body.find(marker)
        if start < 0:
            raise FetchError("Apple hydration data not found")
        rest = body[start + len(marker):]
        end = rest.find('");')
        if end < 0:
            raise FetchError("Apple hydration end marker not found")
        try:
            hydration = json.loads(json.loads(rest[:end + 1]))
        except (json.JSONDecodeError, TypeError) as error:
            raise FetchError(f"Apple hydration could not be decoded: {error}") from error
        search = ((hydration.get("loaderData") or {}).get("search") or {})
        batch = search.get("searchResults") or []
        for raw in batch:
            position_id = str(raw.get("positionId") or "")
            slug = str(raw.get("transformedPostingTitle") or "")
            locations = raw.get("locations") or []
            names = [str(location.get("name")) for location in locations if location.get("name")]
            countries = [str(location.get("countryName")) for location in locations if location.get("countryName")]
            public_url = f"https://jobs.apple.com/en-us/details/{position_id}"
            if slug:
                public_url += f"/{slug}"
            out.append(_job(
                company,
                str(raw.get("postingTitle") or ""),
                " / ".join(names),
                public_url if position_id else "",
                posted=_iso(raw.get("postDateInGMT")),
                country=" / ".join(dict.fromkeys(countries)),
                source_job_id=str(raw.get("reqId") or position_id),
                modality="Remote" if raw.get("homeOffice") else "",
            ))
        if len(batch) < 20 or len(out) >= int(search.get("totalRecords") or 0):
            break
    return out


def _google_payload(body: str):
    marker = "AF_initDataCallback({key: 'ds:1'"
    start = body.find(marker)
    if start < 0:
        raise FetchError("Google ds:1 job payload not found")
    rest = body[start:]
    data_index = rest.find("data:")
    end = rest.find(", sideChannel:", data_index)
    if data_index < 0 or end < 0:
        raise FetchError("Google ds:1 payload markers changed")
    try:
        return json.loads(rest[data_index + len("data:"):end])
    except json.JSONDecodeError as error:
        raise FetchError(f"Google ds:1 payload could not be decoded: {error}") from error


def fetch_google(company: str, max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    for page_number in range(1, max_pages + 1):
        url = (
            "https://www.google.com/about/careers/applications/jobs/results?"
            f"employment_type=INTERN&page={page_number}"
        )
        payload = _google_payload(request(url, headers=_BROWSER_HEADERS, timeout=60))
        if not isinstance(payload, list) or len(payload) < 3:
            raise FetchError("Google ds:1 payload has an unexpected shape")
        batch = payload[0] or []
        total = int(payload[2] or 0)
        for raw in batch:
            if not isinstance(raw, list):
                continue
            job_id = str(raw[0] if len(raw) > 0 and raw[0] is not None else "")
            title = str(raw[1] if len(raw) > 1 and raw[1] is not None else "")
            locations, country = [], ""
            for location in (raw[9] if len(raw) > 9 and isinstance(raw[9], list) else []):
                if not isinstance(location, list):
                    continue
                if location and location[0]:
                    locations.append(str(location[0]))
                if not country and len(location) > 5 and location[5]:
                    country = str(location[5])
            timestamp = raw[12] if len(raw) > 12 and isinstance(raw[12], list) else []
            out.append(_job(
                company,
                title,
                " / ".join(locations),
                f"https://www.google.com/about/careers/applications/jobs/results/{job_id}" if job_id else "",
                posted=_epoch(timestamp[0] if timestamp else None),
                country=country,
                source_job_id=job_id,
            ))
        if len(batch) < 20 or len(out) >= total:
            break
    return out


_META_DOC_ID = "27506805582236862"
_META_FRIENDLY_NAME = "CareersJobSearchResultsDataQuery"


def fetch_meta(company: str) -> list[dict]:
    page_headers = {
        **_BROWSER_HEADERS,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    body = request("https://www.metacareers.com/jobs", headers=page_headers, timeout=60)
    token_match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', body)
    if not token_match:
        raise FetchError("Meta LSD token not found")
    token = token_match.group(1)
    variables = json.dumps({
        "isLoggedIn": False,
        "search_input": {"q": "intern", "page": 1, "results_per_page": None},
        "viewasUserID": None,
    }, separators=(",", ":"))
    form = urlencode({
        "lsd": token,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": _META_FRIENDLY_NAME,
        "variables": variables,
        "server_timestamps": "true",
        "doc_id": _META_DOC_ID,
    }).encode("utf-8")
    headers = {
        "User-Agent": _BROWSER_HEADERS["User-Agent"],
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-FB-LSD": token,
        "X-FB-Friendly-Name": _META_FRIENDLY_NAME,
        "Origin": "https://www.metacareers.com",
        "Referer": "https://www.metacareers.com/jobs",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        data = json.loads(request(
            "https://www.metacareers.com/graphql", method="POST", data=form,
            headers=headers, timeout=60,
        ))
    except json.JSONDecodeError as error:
        raise FetchError(f"Meta GraphQL response was not JSON: {error}") from error
    if data.get("errors"):
        raise FetchError(f"Meta GraphQL error: {data['errors'][0].get('message', 'unknown')}")
    batch = (((data.get("data") or {}).get("job_search_with_featured_jobs") or {}).get("all_jobs") or [])
    out = []
    for raw in batch:
        job_id = str(raw.get("id") or "")
        out.append(_job(
            company,
            str(raw.get("title") or ""),
            " / ".join(raw.get("locations") or []),
            f"https://www.metacareers.com/jobs/{job_id}" if job_id else "",
            source_job_id=job_id,
            description="; ".join((raw.get("teams") or []) + (raw.get("sub_teams") or [])),
        ))
    return out


def _turbo_value(values: list, index: int, depth: int = 8):
    if depth <= 0 or index < 0 or index >= len(values):
        return None
    raw = values[index]
    if isinstance(raw, dict) and raw and all(str(key).startswith("_") for key in raw):
        result = {}
        for key, value_index in raw.items():
            try:
                key_value = values[int(str(key)[1:])]
            except (ValueError, IndexError):
                continue
            if isinstance(key_value, str) and isinstance(value_index, int):
                result[key_value] = _turbo_value(values, value_index, depth - 1)
        return result
    if isinstance(raw, list) and all(isinstance(item, int) for item in raw):
        return [_turbo_value(values, item, depth - 1) for item in raw]
    return raw


def fetch_shopify(company: str) -> list[dict]:
    try:
        values = json.loads(request(
            "https://www.shopify.com/careers.data",
            headers={"Accept": "application/json"},
            timeout=60,
        ))
    except json.JSONDecodeError as error:
        raise FetchError(f"Shopify careers.data was not JSON: {error}") from error
    try:
        key_index = values.index("jobPostingsWithJobs")
    except ValueError as error:
        raise FetchError("Shopify jobPostingsWithJobs marker not found") from error
    reference = f"_{key_index}"
    postings = None
    for raw in values:
        if isinstance(raw, dict) and isinstance(raw.get(reference), int):
            candidate = _turbo_value(values, raw[reference])
            if isinstance(candidate, list):
                postings = candidate
                break
    if postings is None:
        raise FetchError("Shopify posting list could not be resolved")
    out = []
    for item in postings:
        raw = item.get("jobPosting") if isinstance(item, dict) else None
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        out.append(_job(
            company,
            str(raw.get("title") or ""),
            str(raw.get("locationName") or ""),
            str(raw.get("externalLink") or ""),
            posted=_iso(raw.get("publishedDate")),
            description=str(raw.get("teamName") or ""),
            employment_type=str(raw.get("employmentType") or ""),
            source_job_id=str(raw.get("id") or ""),
            modality=str(raw.get("workplaceType") or ""),
        ))
    if not out:
        raise FetchError("Shopify posting list resolved to zero jobs")
    return out


_CITADEL_CARD = re.compile(
    r'(?s)href="(https://[^"]+/careers/details/[^"]+)"[^>]*data-position="([^"]*)"'
    r'.*?careers-listing-card__location">\s*(.*?)\s*</(?:span|div)>'
)


def fetch_citadel(slug: str, company: str, max_pages: int = 10) -> list[dict]:
    host = f"www.{slug}.com"
    out = []
    for page_number in range(1, max_pages + 1):
        form = urlencode({
            "action": "careers_listing_filter",
            "experience-filter": "internships",
            "current_page": page_number,
            "per_page": 100,
            "sort_order": "DESC",
        }).encode("utf-8")
        headers = {
            **_BROWSER_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "Origin": f"https://{host}",
            "Referer": f"https://{host}/careers/open-opportunities/",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            data = json.loads(request(
                f"https://{host}/wp-admin/admin-ajax.php", method="POST", data=form,
                headers=headers, timeout=60,
            ))
        except json.JSONDecodeError as error:
            raise FetchError(f"{company} careers response was not JSON: {error}") from error
        cards = _CITADEL_CARD.findall(str(data.get("content") or ""))
        for link, title, location in cards:
            source_job_id = link.rstrip("/").rsplit("/", 1)[-1]
            out.append(_job(
                company,
                html.unescape(title),
                html_to_text(html.unescape(location)),
                link,
                source_job_id=source_job_id,
            ))
        if not cards or int(data.get("number_of_post") or 0) < 100 or len(out) >= int(data.get("found_posts") or 0):
            break
    return out


def fetch_deshaw(company: str) -> list[dict]:
    body = request("https://www.deshaw.com/careers", headers={"Accept": "text/html"}, timeout=60)
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body, re.DOTALL
    )
    if not match:
        raise FetchError("D. E. Shaw __NEXT_DATA__ payload not found")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise FetchError(f"D. E. Shaw __NEXT_DATA__ could not be decoded: {error}") from error
    page = (((data.get("props") or {}).get("pageProps")) or {})
    out, seen = [], set()
    for raw in (page.get("internships") or []) + (page.get("regularJobs") or []):
        job_id = str(raw.get("id") or "")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        detail = raw.get("data") or {}
        slug = str(detail.get("jobUrl") or "").lower()
        description = ((detail.get("jobDescription") or {}).get("websiteDescription") or "")
        offices = [str(item.get("name")) for item in (raw.get("office") or []) if item.get("name")]
        out.append(_job(
            company,
            str(raw.get("displayName") or ""),
            " / ".join(offices),
            f"https://www.deshaw.com/careers/{slug}" if slug else "",
            posted=_iso(detail.get("validFromDate")),
            description=html_to_text(description),
            source_job_id=job_id,
        ))
    return out


_GOLDMAN_QUERY = """query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      jobTitle
      locations { primary city state country }
      status
      division
      jobType { code description }
      externalSource { sourceId }
      startDate
    }
  }
}"""


def fetch_goldman(company: str, max_pages: int = 20) -> list[dict]:
    out, page_size = [], 100
    for page_number in range(max_pages):
        data = post_json(
            "https://api-higher.gs.com/gateway/api/v1/graphql",
            {
                "operationName": "GetCampusRoles",
                "variables": {"searchQueryInput": {
                    "page": {"pageSize": page_size, "pageNumber": page_number},
                    "searchTerm": "",
                    "experiences": ["CAMPUS", "EARLY_CAREER"],
                }},
                "query": _GOLDMAN_QUERY,
            },
            headers={"Accept": "application/json", "Origin": "https://higher.gs.com"},
            timeout=60,
        )
        if data.get("errors"):
            raise FetchError(f"Goldman GraphQL error: {data['errors'][0].get('message', 'unknown')}")
        result = ((data.get("data") or {}).get("roleSearch") or {})
        batch = result.get("items") or []
        for raw in batch:
            locations = raw.get("locations") or []
            location = next((item for item in locations if item.get("primary")), locations[0] if locations else {})
            region = ", ".join(dict.fromkeys(filter(None, [location.get("city"), location.get("state")])))
            source = raw.get("externalSource") or {}
            source_id = str(source.get("sourceId") or "")
            job_type = raw.get("jobType") or {}
            out.append(_job(
                company,
                str(raw.get("jobTitle") or ""),
                region,
                f"https://higher.gs.com/roles/{source_id}" if source_id else "",
                posted=_iso(raw.get("startDate")),
                country=str(location.get("country") or ""),
                description=str(raw.get("division") or ""),
                employment_type=str(job_type.get("description") or ""),
                source_job_id=str(raw.get("roleId") or ""),
            ))
        if len(batch) < page_size or len(out) >= int(result.get("totalCount") or 0):
            break
    return out


_AVATURE_ARTICLE = re.compile(r'(?s)<article class="article article--result".*?</article>')
_AVATURE_LINK = re.compile(r'(?s)<a class="link" href="([^"]*?/careers/JobDetail/[^"]+)"\s*>(.*?)</a>')
_AVATURE_LOCATION = re.compile(
    r'(?s)<span class="(?:list-item-location|paragraph_inner-span)">(.*?)</span>'
)


def fetch_avature(base: str, company: str, max_pages: int = 60) -> list[dict]:
    separator = "&" if "?" in base else "?"
    out, seen, offset = [], set(), 0
    for _ in range(max_pages):
        body = request(
            f"{base}{separator}jobOffset={offset}",
            headers={"Accept": "text/html"},
            timeout=60,
        )
        articles = _AVATURE_ARTICLE.findall(body)
        if not articles:
            break
        added = 0
        for article in articles:
            link_match = _AVATURE_LINK.search(article)
            if not link_match:
                continue
            link = urljoin(base, html.unescape(link_match.group(1)))
            title = html_to_text(html.unescape(link_match.group(2)))
            source_job_id = link.rstrip("/").rsplit("/", 1)[-1]
            if not source_job_id or source_job_id in seen or not title:
                continue
            seen.add(source_job_id)
            location_match = _AVATURE_LOCATION.search(article)
            location = html_to_text(html.unescape(location_match.group(1))) if location_match else ""
            out.append(_job(company, title, location, link, source_job_id=source_job_id))
            added += 1
        if not added:
            break
        offset += len(articles)
    return out


def fetch_bloomberg(company: str) -> list[dict]:
    return fetch_avature("https://bloomberg.avature.net/careers/SearchJobs/intern/", company)


def fetch_twosigma(company: str) -> list[dict]:
    return fetch_avature("https://careers.twosigma.com/careers/OpenRoles", company)

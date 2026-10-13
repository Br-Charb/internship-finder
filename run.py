"""AWS Lambda entry point for the internship-to-Discord pipeline.

Lambda handler: ``run.lambda_handler``
Local invocation: ``python run.py`` (uses your normal AWS credentials)
Initial no-notification seed: ``python run.py --seed-only``
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jobfinder.match import (
    CATEGORIES,
    Category,
    categorize_job,
    graduation_window,
    is_target_job,
    requirements_snippet,
)
from jobfinder.sources import fetch_all, fetch_description, github_list_revision
from jobfinder.util import FetchError, job_key


ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.toml"
DEFAULT_TABLE = "internship-finder-seen"
CLAIM_LEASE_SECONDS = 15 * 60
DISCORD_TIMEOUT_SECONDS = 15


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class DynamoSeenJobs:
    """Atomic, durable job delivery state backed by a DynamoDB table."""

    def __init__(self, table_name: str) -> None:
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - only relevant outside Lambda
            raise RuntimeError("boto3 is required outside Lambda; run: python -m pip install boto3") from error
        self.resource = boto3.resource("dynamodb")
        self.table = self.resource.Table(table_name)
        self.table_name = table_name

    def known_ids(self, job_ids: list[str]) -> set[str]:
        """Return delivered/seeded jobs and jobs currently claimed by a worker."""
        known: set[str] = set()
        now_epoch = int(time.time())
        for batch in _chunks(list(dict.fromkeys(job_ids)), 100):
            request = {
                self.table_name: {
                    "Keys": [{"job_id": job_id} for job_id in batch],
                    "ProjectionExpression": "job_id, #state, lease_expires",
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ConsistentRead": True,
                }
            }
            unprocessed_attempt = 0
            while request:
                response = self.resource.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    state = item.get("state")
                    lease = int(item.get("lease_expires") or 0)
                    if state in {"delivered", "seeded", "ignored"} or (
                        state == "pending" and lease >= now_epoch
                    ):
                        known.add(item["job_id"])
                request = response.get("UnprocessedKeys") or {}
                if request:
                    time.sleep(min(0.05 * (2 ** unprocessed_attempt), 2.0))
                    unprocessed_attempt += 1
        return known

    @staticmethod
    def _source_key(source_name: str) -> str:
        return "__source_revision__:" + source_name

    def source_revisions(self, source_names: list[str]) -> dict[str, str]:
        """Read cached GitHub-list revisions with the existing BatchGet grant."""
        keys = [self._source_key(name) for name in dict.fromkeys(source_names)]
        revisions: dict[str, str] = {}
        for batch in _chunks(keys, 100):
            request_items = {
                self.table_name: {
                    "Keys": [{"job_id": key} for key in batch],
                    "ProjectionExpression": "job_id, revision",
                    "ConsistentRead": True,
                }
            }
            attempt = 0
            while request_items:
                response = self.resource.batch_get_item(RequestItems=request_items)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    key = str(item.get("job_id") or "")
                    if key.startswith("__source_revision__:") and item.get("revision"):
                        revisions[key.removeprefix("__source_revision__:")] = str(item["revision"])
                request_items = response.get("UnprocessedKeys") or {}
                if request_items:
                    time.sleep(min(0.05 * (2 ** attempt), 2.0))
                    attempt += 1
        return revisions

    def save_source_revision(self, source_name: str, revision: str) -> None:
        now = _iso_now()
        self.table.put_item(Item={
            "job_id": self._source_key(source_name),
            "state": "source_metadata",
            "source_name": source_name,
            "revision": revision,
            "updated_at": now,
        })

    def claim(self, job: dict[str, Any], category: Category) -> str | None:
        """Claim a job unless it is delivered or held by a non-expired worker."""
        token = str(uuid.uuid4())
        now = _utc_now()
        now_epoch = int(now.timestamp())
        try:
            self.table.update_item(
                Key={"job_id": job["key"]},
                UpdateExpression=(
                    "SET #state = :pending, claim_token = :token, lease_expires = :lease, "
                    "first_seen = if_not_exists(first_seen, :now_iso), updated_at = :now_iso, "
                    "job_title = :title, company = :company, category = :category, job_url = :url"
                ),
                ConditionExpression=(
                    "attribute_not_exists(job_id) OR "
                    "(#state = :pending AND lease_expires < :now_epoch)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":pending": "pending",
                    ":token": token,
                    ":lease": now_epoch + CLAIM_LEASE_SECONDS,
                    ":now_epoch": now_epoch,
                    ":now_iso": now.isoformat(timespec="seconds"),
                    ":title": str(job.get("title") or "")[:500],
                    ":company": str(job.get("company") or "")[:300],
                    ":category": category.key,
                    ":url": str(job.get("url") or "")[:1500],
                },
            )
        except Exception as error:  # boto3's concrete exception is runtime-provided
            if _conditional_failure(error):
                return None
            raise
        return token

    def seed(self, job: dict[str, Any], category: Category | None) -> bool:
        """Record a baseline job with one conditional write and no notification."""
        now = _iso_now()
        try:
            self.table.put_item(
                Item={
                    "job_id": job["key"],
                    "state": "seeded",
                    "first_seen": now,
                    "delivered_at": now,
                    "updated_at": now,
                    "job_title": str(job.get("title") or "")[:500],
                    "company": str(job.get("company") or "")[:300],
                    "category": category.key if category else "uncategorized",
                    "job_url": str(job.get("url") or "")[:1500],
                },
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except Exception as error:
            if _conditional_failure(error):
                return False
            raise
        return True

    def ignore(self, job: dict[str, Any]) -> bool:
        """Remember an uncategorized job so every poll does not process it again."""
        now = _iso_now()
        try:
            self.table.put_item(
                Item={
                    "job_id": job["key"],
                    "state": "ignored",
                    "first_seen": now,
                    "updated_at": now,
                    "job_title": str(job.get("title") or "")[:500],
                    "company": str(job.get("company") or "")[:300],
                    "category": "uncategorized",
                    "job_url": str(job.get("url") or "")[:1500],
                },
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except Exception as error:
            if _conditional_failure(error):
                return False
            raise
        return True

    def complete(self, job_id: str, token: str, state: str = "delivered") -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=(
                "SET #state = :state, delivered_at = :now, updated_at = :now "
                "REMOVE claim_token, lease_expires"
            ),
            ConditionExpression="claim_token = :token",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":state": state, ":now": _iso_now(), ":token": token},
        )

    def release(self, job_id: str, token: str) -> None:
        """Delete only this invocation's failed claim so a later run can retry."""
        try:
            self.table.delete_item(
                Key={"job_id": job_id},
                ConditionExpression="#state = :pending AND claim_token = :token",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":pending": "pending", ":token": token},
            )
        except Exception as error:
            if not _conditional_failure(error):
                raise


def _webhook_urls() -> dict[str, str]:
    urls: dict[str, str] = {}
    missing: list[str] = []
    for category in CATEGORIES:
        value = os.environ.get(category.webhook_env, "").strip()
        if not value:
            missing.append(category.webhook_env)
            continue
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "discord.com" or host.endswith(".discord.com")
                                              or host == "discordapp.com"):
            raise ValueError(f"{category.webhook_env} is not a Discord HTTPS webhook URL")
        urls[category.key] = value
    if missing:
        raise ValueError("Missing Discord webhook environment variables: " + ", ".join(missing))
    return urls


def _with_wait_parameter(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["wait"] = "true"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    urllib.parse.urlencode(query), parsed.fragment))


def _truncate(value: str, limit: int) -> str:
    value = value.strip() or "Unknown"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rsplit(" ", 1)[0] + "…"


def discord_payload(job: dict[str, Any], category: Category, matched_terms: list[str]) -> dict[str, Any]:
    posted = str(job.get("posted") or "date not supplied")
    source = str(job.get("source") or "ATS source")
    description = str(job.get("description") or "")
    return {
        "username": "Internship Finder",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": _truncate(str(job.get("title") or "Untitled role"), 256),
            "url": str(job["url"]),
            "color": category.color,
            "fields": [
                {"name": "Company", "value": _truncate(str(job.get("company") or "Unknown"), 1024), "inline": True},
                {"name": "Graduation", "value": graduation_window(description), "inline": True},
                {"name": "Location", "value": _truncate(str(job.get("location") or "Not listed"), 1024), "inline": False},
                {"name": "Category", "value": category.label, "inline": True},
                {"name": "Posted", "value": posted, "inline": True},
                {"name": "Requirements snapshot", "value": requirements_snippet(description), "inline": False},
                {"name": "Apply", "value": f"[Open the job posting]({job['url']})", "inline": False},
            ],
            "footer": {"text": _truncate(f"{source} • Matched: {', '.join(matched_terms[:6])}", 2048)},
            "timestamp": _iso_now(),
        }],
    }


def post_to_discord(webhook_url: str, payload: dict[str, Any], attempts: int = 4) -> None:
    """Post one embed, respecting Discord's 429 retry response."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = _with_wait_parameter(webhook_url)
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "internship-finder-lambda/2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=DISCORD_TIMEOUT_SECONDS) as response:
                if response.status in {200, 204}:
                    return
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            response_body = error.read()
        except urllib.error.URLError as error:
            if attempt == attempts - 1:
                raise RuntimeError(f"Discord webhook network error: {error.reason}") from error
            time.sleep(2 ** attempt)
            continue

        if status == 429 and attempt < attempts - 1:
            try:
                retry_after = float(json.loads(response_body or b"{}").get("retry_after", 1))
            except (TypeError, ValueError, json.JSONDecodeError):
                retry_after = 1
            time.sleep(min(max(retry_after, 0.25), 30))
            continue
        if 500 <= status < 600 and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Discord webhook returned HTTP {status}")
    raise RuntimeError("Discord webhook retries exhausted")


def test_webhooks() -> dict[str, Any]:
    """Send one clearly labeled test embed to every configured Discord channel."""
    urls = _webhook_urls()
    sent: list[str] = []
    failures: list[str] = []
    for category in CATEGORIES:
        job = {
            "company": "Internship Finder",
            "title": f"Webhook test — {category.label}",
            "location": "AWS Lambda (us-east-1)",
            "url": "https://github.com/Br-Charb/internship-finder",
            "source": "Manual Lambda connection test",
            "posted": _utc_now().date().isoformat(),
            "description": "Requirements:\n- No action needed; this is only a webhook connection test.",
        }
        try:
            post_to_discord(urls[category.key], discord_payload(job, category, ["connection test"]))
            sent.append(category.key)
        except Exception as error:
            failures.append(f"{category.key}: {error}")

    if failures:
        raise RuntimeError("Webhook test failures: " + "; ".join(failures))
    return {"webhook_tests_sent": sent, "count": len(sent)}


def _deduplicate(raw_jobs: list[dict[str, Any]], scope: str) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for job in raw_jobs:
        title = str(job.get("title") or "").strip()
        url = str(job.get("url") or "").strip()
        if not title or not url or not is_target_job(title, scope, str(job.get("employment_type") or "")):
            continue
        job["key"] = job_key(url, str(job.get("company") or ""), title)
        existing = jobs.get(job["key"])
        if existing is None or (not existing.get("description") and job.get("description")):
            jobs[job["key"]] = job
    return jobs


def _fill_descriptions(jobs: list[dict[str, Any]], limit: int) -> int:
    missing = [job for job in jobs if not job.get("description")][:limit]

    def load(job: dict[str, Any]) -> tuple[dict[str, Any], str]:
        try:
            return job, fetch_description(str(job["url"]))
        except (FetchError, ValueError):
            return job, ""

    loaded = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for job, description in pool.map(load, missing):
            if description:
                job["description"] = description
                loaded += 1
    return loaded


def _active_tiers(now: dt.datetime | None = None) -> set[int]:
    """Select polling tiers while keeping EventBridge itself at five minutes."""
    now = now or _utc_now()
    five_minute_slot = int(now.timestamp()) // (5 * 60)
    tiers = {1}
    if five_minute_slot % 3 == 0:
        tiers.add(2)
    if five_minute_slot % 12 == 0:
        tiers.add(3)
    return tiers


def _skip_unchanged_lists(
    config: dict[str, Any], state: DynamoSeenJobs
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Remove unchanged raw GitHub lists and return revisions to commit later."""
    lists = config.get("list", [])
    if not lists:
        return config, {}, []

    previous = state.source_revisions([str(src["name"]) for src in lists])
    pending: dict[str, str] = {}
    cached: list[str] = []
    keep: list[dict[str, Any]] = []
    for src in lists:
        name = str(src["name"])
        revision = github_list_revision(src)
        if revision and previous.get(name) == revision:
            cached.append(name)
            continue
        keep.append(src)
        if revision:
            pending[name] = revision

    filtered = copy.deepcopy(config)
    filtered["list"] = keep
    return filtered, pending, cached


def execute(seed_only: bool = False) -> dict[str, Any]:
    """Fetch, route, deliver, and persist one complete scan."""
    config = tomllib.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    scope = os.environ.get("JOB_SCOPE", "internships")
    description_limit = max(0, int(os.environ.get("MAX_DESCRIPTION_LOOKUPS", "100")))
    delivery_limit = max(1, int(os.environ.get("MAX_JOBS_PER_RUN", "40")))
    table_name = os.environ.get("DYNAMODB_TABLE", DEFAULT_TABLE).strip()
    if not table_name:
        raise ValueError("DYNAMODB_TABLE cannot be empty")

    webhooks = {} if seed_only else _webhook_urls()
    state = DynamoSeenJobs(table_name)

    tiers = {1, 2, 3} if seed_only else _active_tiers()
    config, pending_revisions, cached_lists = _skip_unchanged_lists(config, state)

    print(f"Fetching sources from {SOURCES_FILE.name}; active tiers={sorted(tiers)}")
    raw_jobs, source_status = fetch_all(config, tiers=tiers)
    jobs_by_id = _deduplicate(raw_jobs, scope)
    known = state.known_ids(list(jobs_by_id))
    new_jobs = [job for key, job in jobs_by_id.items() if key not in known]
    new_jobs.sort(key=lambda job: (str(job.get("posted") or ""), str(job.get("company") or "")), reverse=True)
    # A baseline never sends notifications, so fetching thousands of full job
    # pages would add time and network cost without changing user-visible output.
    loaded = 0 if seed_only else _fill_descriptions(new_jobs, description_limit)

    routed: list[tuple[dict[str, Any], Category, list[str]]] = []
    unmatched: list[dict[str, Any]] = []
    for job in new_jobs:
        category, terms = categorize_job(str(job.get("title") or ""), str(job.get("description") or ""))
        if category:
            routed.append((job, category, terms))
        else:
            unmatched.append(job)

    delivered = seeded = ignored = skipped_claim = failures = 0
    if seed_only:
        categories = {job["key"]: category for job, category, _terms in routed}
        for job in new_jobs:
            category = categories.get(job["key"])
            if state.seed(job, category):
                seeded += 1
                if category is None:
                    ignored += 1
            else:
                skipped_claim += 1
    else:
        for job in unmatched:
            if state.ignore(job):
                ignored += 1
            else:
                skipped_claim += 1

        for job, category, terms in routed[:delivery_limit]:
            token = state.claim(job, category)
            if token is None:
                skipped_claim += 1
                continue

            try:
                post_to_discord(webhooks[category.key], discord_payload(job, category, terms))
            except Exception as error:  # continue other categories and retry this job next invocation
                failures += 1
                print(f"ERROR delivery {job['key']} ({category.key}): {error}", file=sys.stderr)
                state.release(job["key"], token)
                continue

            # Do not release the claim if this write fails: Discord already accepted
            # the post, and keeping the lease is safer than immediately duplicating it.
            state.complete(job["key"], token)
            delivered += 1

    failed_sources = [status["name"] for status in source_status if not status["ok"]]
    deferred = 0 if seed_only else max(0, len(routed) - delivery_limit)
    summary = {
        "active_tiers": sorted(tiers),
        "raw_jobs": len(raw_jobs),
        "eligible_unique_jobs": len(jobs_by_id),
        "previously_seen": len(known),
        "unseen": len(new_jobs),
        "descriptions_loaded": loaded,
        "categorized": len(routed),
        "delivered": delivered,
        "seeded": seeded,
        "ignored": ignored,
        "claim_conflicts": skipped_claim,
        "delivery_failures": failures,
        "deferred_by_limit": deferred,
        "unchanged_lists_skipped": cached_lists,
        "failed_sources": failed_sources,
    }

    # Only mark a list revision consumed after its fetch succeeded and all
    # notifications from this pass were handled. Otherwise the same revision is
    # retried, preserving delivery backlog and recovery from transient errors.
    source_ok = {status["name"]: status["ok"] for status in source_status}
    if not failures and not deferred:
        for source_name, revision in pending_revisions.items():
            if source_ok.get(source_name):
                state.save_source_revision(source_name, revision)
    print(json.dumps(summary, sort_keys=True))
    if failures:
        raise RuntimeError(f"{failures} Discord delivery attempt(s) failed; released for retry")
    return summary


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """AWS Lambda handler with baseline and webhook-test modes."""
    event = event or {}
    if event.get("test_webhooks"):
        if event.get("seed_only"):
            raise ValueError("test_webhooks and seed_only cannot both be true")
        return test_webhooks()
    seed_only = bool(event.get("seed_only", False))
    return execute(seed_only=seed_only)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch internship postings and route new ones to Discord.")
    parser.add_argument("--seed-only", action="store_true", help="record current matches without notifying Discord")
    args = parser.parse_args()
    execute(seed_only=args.seed_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

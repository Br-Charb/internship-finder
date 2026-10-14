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
from zoneinfo import ZoneInfo

from jobfinder.match import (
    CATEGORIES,
    Category,
    categorize_job,
    is_phd_exclusive_job,
    is_recent_posting,
    is_target_job,
    is_united_states_job,
    requirements_snippet,
)
from jobfinder.sources import fetch_all, fetch_description, github_list_revision
from jobfinder.util import FetchError, job_key


ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.toml"
DEFAULT_TABLE = "internship-finder-seen"
CLAIM_LEASE_SECONDS = 15 * 60
DISCORD_TIMEOUT_SECONDS = 15
DEFAULT_HEALTH_FAILURE_THRESHOLD = 6
DEFAULT_HEALTH_RATE_LIMIT_THRESHOLD = 12
DEFAULT_HEALTH_RECOVERY_THRESHOLD = 2
DEFAULT_HEALTH_HEARTBEAT_HOUR = 8
DEFAULT_HEALTH_TIME_ZONE = "America/New_York"


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

    @staticmethod
    def _health_key(source_name: str) -> str:
        return "__source_health__:" + source_name

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

    def source_health(self, source_names: list[str]) -> dict[str, dict[str, Any]]:
        """Read persistent health counters using the table's existing BatchGet grant."""
        keys = [self._health_key(name) for name in dict.fromkeys(source_names)]
        health: dict[str, dict[str, Any]] = {}
        for batch in _chunks(keys, 100):
            request_items = {
                self.table_name: {
                    "Keys": [{"job_id": key} for key in batch],
                    "ConsistentRead": True,
                }
            }
            attempt = 0
            while request_items:
                response = self.resource.batch_get_item(RequestItems=request_items)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    key = str(item.get("job_id") or "")
                    if key.startswith("__source_health__:"):
                        health[key.removeprefix("__source_health__:")] = item
                request_items = response.get("UnprocessedKeys") or {}
                if request_items:
                    time.sleep(min(0.05 * (2 ** attempt), 2.0))
                    attempt += 1
        return health

    def save_source_health(self, source_name: str, values: dict[str, Any]) -> None:
        item = {
            "job_id": self._health_key(source_name),
            "state": "source_health",
            "source_name": source_name,
            "updated_at": _iso_now(),
            **values,
        }
        self.table.put_item(Item=item)

    def claim_daily_heartbeat(self, day: str) -> bool:
        """Atomically reserve one health heartbeat per configured local day."""
        try:
            self.table.update_item(
                Key={"job_id": "__health_heartbeat__"},
                UpdateExpression="SET #state = :state, heartbeat_date = :day, updated_at = :now",
                ConditionExpression="attribute_not_exists(heartbeat_date) OR heartbeat_date <> :day",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": "health_metadata",
                    ":day": day,
                    ":now": _iso_now(),
                },
            )
        except Exception as error:
            if _conditional_failure(error):
                return False
            raise
        return True

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

    def ignore(self, job: dict[str, Any], reason: str = "uncategorized") -> bool:
        """Remember a rejected job so every poll does not process it again."""
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
                    "category": reason,
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


def _validate_webhook_url(value: str, variable_name: str) -> str:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "discord.com" or host.endswith(".discord.com") or host == "discordapp.com"
    ):
        raise ValueError(f"{variable_name} is not a Discord HTTPS webhook URL")
    return value


def _webhook_urls() -> dict[str, str]:
    urls: dict[str, str] = {}
    missing: list[str] = []
    for category in CATEGORIES:
        value = os.environ.get(category.webhook_env, "").strip()
        if not value:
            missing.append(category.webhook_env)
            continue
        urls[category.key] = _validate_webhook_url(value, category.webhook_env)
    if missing:
        raise ValueError("Missing Discord webhook environment variables: " + ", ".join(missing))
    return urls


def _health_webhook_url() -> str | None:
    value = os.environ.get("DISCORD_WEBHOOK_HEALTH", "").strip()
    return _validate_webhook_url(value, "DISCORD_WEBHOOK_HEALTH") if value else None


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
    return {
        "webhook_tests_sent": sent,
        "count": len(sent),
    }


def _health_payload(title: str, description: str, color: int) -> dict[str, Any]:
    return {
        "username": "Internship Finder Health",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": _truncate(title, 256),
            "description": _truncate(description, 4096),
            "color": color,
            "timestamp": _iso_now(),
            "footer": {"text": "AWS Lambda source monitor"},
        }],
    }


def _heartbeat_due() -> tuple[bool, str, int, str]:
    """Return whether today's heartbeat is due in the configured time zone."""
    time_zone = os.environ.get("HEALTH_TIME_ZONE", DEFAULT_HEALTH_TIME_ZONE).strip()
    if not time_zone:
        time_zone = DEFAULT_HEALTH_TIME_ZONE
    try:
        hour = int(os.environ.get(
            "HEALTH_HEARTBEAT_HOUR", str(DEFAULT_HEALTH_HEARTBEAT_HOUR)
        ))
    except ValueError as error:
        raise ValueError("HEALTH_HEARTBEAT_HOUR must be an integer from 0 through 23") from error
    if not 0 <= hour <= 23:
        raise ValueError("HEALTH_HEARTBEAT_HOUR must be an integer from 0 through 23")

    local_now = _utc_now().astimezone(ZoneInfo(time_zone))
    return local_now.hour >= hour, local_now.date().isoformat(), hour, time_zone


def test_health_webhook() -> dict[str, Any]:
    webhook = _health_webhook_url()
    if webhook is None:
        raise ValueError("Missing Discord webhook environment variable: DISCORD_WEBHOOK_HEALTH")
    post_to_discord(
        webhook,
        _health_payload(
            "Health channel connected",
            "The internship scraper can send source failures, recoveries, and daily heartbeats here.",
            0x2ECC71,
        ),
    )
    return {"health_webhook_test": "sent"}


def _monitor_source_health(
    state: DynamoSeenJobs,
    source_status: list[dict[str, Any]],
    webhook: str,
    summary: dict[str, Any],
) -> dict[str, int]:
    """Persist source streaks and send one compact health digest when needed."""
    failure_threshold = max(
        1,
        int(os.environ.get("HEALTH_FAILURE_THRESHOLD", str(DEFAULT_HEALTH_FAILURE_THRESHOLD))),
    )
    rate_limit_threshold = max(
        failure_threshold,
        int(os.environ.get(
            "HEALTH_RATE_LIMIT_THRESHOLD", str(DEFAULT_HEALTH_RATE_LIMIT_THRESHOLD)
        )),
    )
    recovery_threshold = max(
        1,
        int(os.environ.get(
            "HEALTH_RECOVERY_THRESHOLD", str(DEFAULT_HEALTH_RECOVERY_THRESHOLD)
        )),
    )
    rate_limit_prone = {"Citadel", "Citadel Securities", "Microsoft", "Meta", "LinkedIn"}
    previous = state.source_health([str(status["name"]) for status in source_status])
    changes: list[tuple[str, dict[str, Any], str | None]] = []
    alerts: list[str] = []
    recoveries = 0

    for status in source_status:
        name = str(status["name"])
        prior = previous.get(name, {})
        failures = int(prior.get("failure_streak") or 0)
        recovery_streak = int(prior.get("recovery_streak") or 0)
        failure_alerted = bool(prior.get("failure_alerted", False))
        ever_nonzero = bool(prior.get("ever_nonzero", False))
        event_kind: str | None = None
        should_save = False
        error = str(status.get("error") or "")
        threshold = (
            rate_limit_threshold
            if name in rate_limit_prone or "HTTP 429" in error or "HTTP 403" in error
            else failure_threshold
        )

        if not status.get("ok"):
            failures = min(failures + 1, threshold)
            recovery_streak = 0
            should_save = (
                failures != int(prior.get("failure_streak") or 0)
                or int(prior.get("recovery_streak") or 0) != 0
                or bool(prior.get("zero_alerted", False))
            )
            if failures >= threshold and not failure_alerted:
                event_kind = "failure"
                failure_alerted = True
                shown_error = _truncate(error or "unknown error", 300)
                alerts.append(
                    f"🔴 **{name}** failed {failures} consecutive checks — `{shown_error}`"
                )
        else:
            count = int(status.get("count") or 0)
            if failure_alerted:
                recovery_streak = min(recovery_streak + 1, recovery_threshold)
                should_save = True
                if recovery_streak >= recovery_threshold:
                    event_kind = "recovery"
                    recoveries += 1
                    alerts.append(
                        f"🟢 **{name}** recovered after {recovery_streak} consecutive "
                        f"successful checks and returned {count} jobs."
                    )
                    failure_alerted = False
                    failures = recovery_streak = 0
            else:
                should_save = bool(prior) and (
                    failures > 0
                    or recovery_streak > 0
                    or bool(prior.get("zero_alerted", False))
                    or int(prior.get("zero_streak") or 0) > 0
                )
                failures = recovery_streak = 0
            if count > 0:
                ever_nonzero = True

        if should_save or event_kind:
            changes.append((name, {
                "failure_streak": failures,
                "recovery_streak": recovery_streak,
                "zero_streak": 0,
                "failure_alerted": failure_alerted,
                "zero_alerted": False,
                "ever_nonzero": ever_nonzero,
                "last_count": int(status.get("count") or 0),
                "last_error": str(status.get("error") or "")[:500],
            }, event_kind))

    delivery_failed = 0
    if alerts:
        try:
            post_to_discord(
                webhook,
                _health_payload("Source health update", "\n".join(alerts[:25]), 0xE67E22),
            )
        except Exception as error:
            delivery_failed = 1
            print(f"ERROR health webhook: {error}", file=sys.stderr)
            # Failed alerts remain unacknowledged so the next eligible check retries them.
            for _name, values, event_kind in changes:
                if event_kind == "failure":
                    values["failure_alerted"] = False
                elif event_kind == "recovery":
                    values["failure_alerted"] = True
                    values["recovery_streak"] = recovery_threshold - 1

    for name, values, _event_kind in changes:
        state.save_source_health(name, values)

    heartbeat_sent = 0
    heartbeat_is_due, local_day, heartbeat_hour, heartbeat_time_zone = _heartbeat_due()
    if heartbeat_is_due and state.claim_daily_heartbeat(local_day):
        heartbeat_description = (
            f"The scheduled pipeline is running. This invocation checked "
            f"**{summary['sources_checked']}** sources, found **{summary['raw_jobs']}** raw jobs, "
            f"and delivered **{summary['delivered']}** new alerts.\n\n"
            f"Sources failing this check: **{len(summary['failed_sources'])}**. "
            "Individual source alerts require consecutive failures.\n\n"
            f"Scheduled after **{heartbeat_hour:02d}:00 {heartbeat_time_zone}** each day."
        )
        try:
            post_to_discord(
                webhook,
                _health_payload("Daily pipeline heartbeat", heartbeat_description, 0x3498DB),
            )
            heartbeat_sent = 1
        except Exception as error:
            delivery_failed += 1
            print(f"ERROR health heartbeat: {error}", file=sys.stderr)

    return {
        "health_events": len(alerts),
        "health_recoveries": recoveries,
        "health_heartbeat_sent": heartbeat_sent,
        "health_delivery_failures": delivery_failed,
    }


def _deduplicate(raw_jobs: list[dict[str, Any]], scope: str) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for job in raw_jobs:
        title = str(job.get("title") or "").strip()
        url = str(job.get("url") or "").strip()
        if not title or not url or not is_target_job(title, scope, str(job.get("employment_type") or "")):
            continue
        job["key"] = job_key(
            url,
            str(job.get("company") or ""),
            title,
            source=str(job.get("source_id") or job.get("source") or ""),
            source_job_id=str(job.get("source_job_id") or ""),
        )
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


def _five_minute_slot(now: dt.datetime | None = None) -> int:
    """Return the stable EventBridge slot used to stagger the source catalog."""
    now = now or _utc_now()
    return int(now.timestamp()) // (5 * 60)


def _skip_unchanged_lists(
    config: dict[str, Any], state: DynamoSeenJobs, *, force: bool = False
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Remove unchanged raw GitHub lists and return revisions to commit later."""
    lists = config.get("list", [])
    if not lists:
        return config, {}, []

    previous = {} if force else state.source_revisions([str(src["name"]) for src in lists])
    pending: dict[str, str] = {}
    cached: list[str] = []
    keep: list[dict[str, Any]] = []
    for src in lists:
        name = str(src["name"])
        revision = github_list_revision(src)
        if not force and revision and previous.get(name) == revision:
            cached.append(name)
            continue
        keep.append(src)
        if revision:
            pending[name] = revision

    filtered = copy.deepcopy(config)
    filtered["list"] = keep
    return filtered, pending, cached


def _has_known_recent_posting_date(posted: str, max_age_days: int) -> bool:
    """Require a parseable date for a historical backfill."""
    try:
        dt.date.fromisoformat(str(posted)[:10])
    except ValueError:
        return False
    return is_recent_posting(posted, max_age_days=max_age_days)


def _execute_category_backfill(
    *,
    state: DynamoSeenJobs,
    jobs_by_id: dict[str, dict[str, Any]],
    source_status: list[dict[str, Any]],
    pending_revisions: dict[str, str],
    cached_lists: list[str],
    raw_job_count: int,
    target_category: Category,
    webhooks: dict[str, str],
    description_limit: int,
    delivery_limit: int,
    max_age_days: int,
    source_partition: tuple[int, int] | None,
) -> dict[str, Any]:
    """Deliver a category's recent history without disturbing normal job IDs.

    Synthetic DynamoDB keys make this operation idempotent and independent of
    the original catalog seed. At the same time, newly added source catalogs
    are baselined under their normal keys so the next scheduled scan cannot
    replay non-accounting jobs from those sources.
    """
    all_jobs = list(jobs_by_id.values())
    globally_known = state.known_ids(list(jobs_by_id))
    global_new = [job for job in all_jobs if job["key"] not in globally_known]

    phd_exclusive: list[dict[str, Any]] = []
    outside_us: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    unknown_posted: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for job in all_jobs:
        if is_phd_exclusive_job(str(job.get("title") or ""), str(job.get("description") or "")):
            phd_exclusive.append(job)
        elif not is_united_states_job(
            str(job.get("location") or ""), str(job.get("country") or "")
        ):
            outside_us.append(job)
        elif not str(job.get("posted") or ""):
            unknown_posted.append(job)
        elif not _has_known_recent_posting_date(str(job.get("posted") or ""), max_age_days):
            stale.append(job)
        else:
            candidates.append(job)

    loaded = _fill_descriptions(candidates, description_limit)
    eligible_candidates: list[dict[str, Any]] = []
    for job in candidates:
        if is_phd_exclusive_job(str(job.get("title") or ""), str(job.get("description") or "")):
            phd_exclusive.append(job)
        else:
            eligible_candidates.append(job)
    candidates = eligible_candidates

    classified: dict[str, Category] = {}
    routed: list[tuple[dict[str, Any], Category, list[str]]] = []
    for job in candidates:
        category, terms = categorize_job(
            str(job.get("title") or ""), str(job.get("description") or "")
        )
        if category:
            classified[job["key"]] = category
            if category.key == target_category.key:
                routed.append((job, category, terms))

    # Record every newly introduced normal job before the scheduler is enabled.
    seeded = ignored = claim_conflicts = 0
    for job in global_new:
        category = classified.get(job["key"])
        if state.seed(job, category):
            seeded += 1
            if category is None:
                ignored += 1
        else:
            claim_conflicts += 1

    backfill_rows: list[tuple[dict[str, Any], Category, list[str]]] = []
    for job, category, terms in routed:
        synthetic = dict(job)
        synthetic["key"] = f"__backfill__:{target_category.key}:{job['key']}"
        backfill_rows.append((synthetic, category, terms))
    backfill_rows.sort(
        key=lambda row: (str(row[0].get("posted") or ""), str(row[0].get("company") or "")),
        reverse=True,
    )
    already_delivered = state.known_ids([job["key"] for job, _category, _terms in backfill_rows])
    pending = [row for row in backfill_rows if row[0]["key"] not in already_delivered]

    delivered = failures = 0
    for job, category, terms in pending[:delivery_limit]:
        token = state.claim(job, category)
        if token is None:
            claim_conflicts += 1
            continue
        try:
            post_to_discord(webhooks[category.key], discord_payload(job, category, terms))
        except Exception as error:
            failures += 1
            print(f"ERROR backfill delivery {job['key']}: {error}", file=sys.stderr)
            state.release(job["key"], token)
            continue
        state.complete(job["key"], token)
        delivered += 1

    failed_sources = [status["name"] for status in source_status if not status["ok"]]
    deferred = max(0, len(pending) - delivery_limit)
    summary = {
        "active_tiers": sorted({int(status["tier"]) for status in source_status}),
        "schedule_slot": None,
        "seed_partition": (
            {"index": source_partition[0], "count": source_partition[1]}
            if source_partition is not None else None
        ),
        "backfill_category": target_category.key,
        "sources_checked": len(source_status),
        "raw_jobs": raw_job_count,
        "eligible_unique_jobs": len(jobs_by_id),
        "previously_seen": len(globally_known),
        "unseen": len(global_new),
        "us_recent_candidates": len(candidates),
        "phd_exclusive": len({job["key"] for job in phd_exclusive}),
        "outside_us_or_unknown": len(outside_us),
        "stale": len(stale),
        "unknown_posting_date": len(unknown_posted),
        "max_posting_age_days": max_age_days,
        "descriptions_loaded": loaded,
        "categorized": len(routed),
        "backfill_previously_delivered": len(already_delivered),
        "delivered": delivered,
        "seeded": seeded,
        "ignored": ignored,
        "claim_conflicts": claim_conflicts,
        "delivery_failures": failures,
        "deferred_by_limit": deferred,
        "unchanged_lists_skipped": cached_lists,
        "failed_sources": failed_sources,
        "health_events": 0,
        "health_recoveries": 0,
        "health_heartbeat_sent": 0,
        "health_delivery_failures": 0,
    }

    source_ok = {status["name"]: status["ok"] for status in source_status}
    if not failures and not deferred:
        for source_name, revision in pending_revisions.items():
            if source_ok.get(source_name):
                state.save_source_revision(source_name, revision)
    print(json.dumps(summary, sort_keys=True))
    if failures:
        raise RuntimeError(f"{failures} Discord backfill attempt(s) failed; released for retry")
    return summary


def execute(
    seed_only: bool = False,
    only_sources: set[str] | None = None,
    seed_partition: tuple[int, int] | None = None,
    backfill_category: str | None = None,
) -> dict[str, Any]:
    """Fetch, route, deliver, and persist one complete scan."""
    config = tomllib.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    scope = os.environ.get("JOB_SCOPE", "internships")
    description_limit = max(0, int(os.environ.get("MAX_DESCRIPTION_LOOKUPS", "100")))
    delivery_limit = max(1, int(os.environ.get("MAX_JOBS_PER_RUN", "40")))
    max_age_days = max(0, int(os.environ.get("MAX_POSTING_AGE_DAYS", "14")))
    table_name = os.environ.get("DYNAMODB_TABLE", DEFAULT_TABLE).strip()
    if not table_name:
        raise ValueError("DYNAMODB_TABLE cannot be empty")

    if seed_only and backfill_category:
        raise ValueError("seed_only and backfill_category cannot be combined")
    target_category = next(
        (category for category in CATEGORIES if category.key == backfill_category), None
    )
    if backfill_category and target_category is None:
        raise ValueError(f"Unknown backfill category: {backfill_category}")

    webhooks = {} if seed_only else _webhook_urls()
    state = DynamoSeenJobs(table_name)

    tiers = {1, 2, 3}
    schedule_slot = None if seed_only or only_sources or backfill_category else _five_minute_slot()
    config, pending_revisions, cached_lists = _skip_unchanged_lists(
        config, state, force=seed_only or bool(backfill_category)
    )

    scope_note = f" selected={sorted(only_sources)}" if only_sources else ""
    if seed_partition is not None:
        scope_note += f" seed_partition={seed_partition[0] + 1}/{seed_partition[1]}"
    print(f"Fetching sources from {SOURCES_FILE.name}; slot={schedule_slot}{scope_note}")
    raw_jobs, source_status = fetch_all(
        config,
        tiers=tiers,
        only=only_sources,
        schedule_slot=schedule_slot,
        partition=seed_partition,
    )
    jobs_by_id = _deduplicate(raw_jobs, scope)
    if target_category is not None:
        return _execute_category_backfill(
            state=state,
            jobs_by_id=jobs_by_id,
            source_status=source_status,
            pending_revisions=pending_revisions,
            cached_lists=cached_lists,
            raw_job_count=len(raw_jobs),
            target_category=target_category,
            webhooks=webhooks,
            description_limit=description_limit,
            delivery_limit=delivery_limit,
            max_age_days=max_age_days,
            source_partition=seed_partition,
        )
    known = state.known_ids(list(jobs_by_id))
    new_jobs = [job for key, job in jobs_by_id.items() if key not in known]
    new_jobs.sort(key=lambda job: (str(job.get("posted") or ""), str(job.get("company") or "")), reverse=True)

    phd_exclusive: list[dict[str, Any]] = []
    outside_us: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for job in new_jobs:
        if is_phd_exclusive_job(
            str(job.get("title") or ""), str(job.get("description") or "")
        ):
            phd_exclusive.append(job)
        elif not is_united_states_job(
            str(job.get("location") or ""),
            str(job.get("country") or ""),
        ):
            outside_us.append(job)
        elif not is_recent_posting(str(job.get("posted") or ""), max_age_days=max_age_days):
            stale.append(job)
        else:
            candidates.append(job)

    # A baseline never sends notifications, so fetching thousands of full job
    # pages would add time and network cost without changing user-visible output.
    loaded = 0 if seed_only else _fill_descriptions(candidates, description_limit)
    if not seed_only:
        eligible_candidates: list[dict[str, Any]] = []
        for job in candidates:
            if is_phd_exclusive_job(
                str(job.get("title") or ""), str(job.get("description") or "")
            ):
                phd_exclusive.append(job)
            else:
                eligible_candidates.append(job)
        candidates = eligible_candidates

    routed: list[tuple[dict[str, Any], Category, list[str]]] = []
    unmatched: list[dict[str, Any]] = []
    for job in candidates:
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
        for job in phd_exclusive:
            if state.ignore(job, "phd_exclusive"):
                ignored += 1
            else:
                skipped_claim += 1

        for job in outside_us:
            if state.ignore(job, "outside_us"):
                ignored += 1
            else:
                skipped_claim += 1

        for job in stale:
            if state.ignore(job, "stale"):
                ignored += 1
            else:
                skipped_claim += 1

        for job in unmatched:
            if state.ignore(job, "uncategorized"):
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
        "active_tiers": sorted({int(status["tier"]) for status in source_status}),
        "schedule_slot": schedule_slot,
        "seed_partition": (
            {"index": seed_partition[0], "count": seed_partition[1]}
            if seed_partition is not None else None
        ),
        "sources_checked": len(source_status),
        "raw_jobs": len(raw_jobs),
        "eligible_unique_jobs": len(jobs_by_id),
        "previously_seen": len(known),
        "unseen": len(new_jobs),
        "us_recent_candidates": len(candidates),
        "phd_exclusive": len(phd_exclusive),
        "outside_us_or_unknown": len(outside_us),
        "stale": len(stale),
        "max_posting_age_days": max_age_days,
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

    health_summary = {
        "health_events": 0,
        "health_recoveries": 0,
        "health_heartbeat_sent": 0,
        "health_delivery_failures": 0,
    }
    if not seed_only:
        try:
            health_webhook = _health_webhook_url()
            if health_webhook:
                health_summary = _monitor_source_health(
                    state, source_status, health_webhook, summary
                )
        except Exception as error:
            # Monitoring must never stop job notifications or mark them failed.
            health_summary["health_delivery_failures"] += 1
            print(f"ERROR source health monitor: {error}", file=sys.stderr)
    summary.update(health_summary)

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
    raw_backfill_category = event.get("backfill_category")
    if raw_backfill_category is not None and not isinstance(raw_backfill_category, str):
        raise ValueError("backfill_category must be a category name")
    backfill_category = (
        raw_backfill_category.strip().lower() if raw_backfill_category else None
    )
    if event.get("test_health_webhook"):
        if event.get("test_webhooks") or event.get("seed_only") or backfill_category:
            raise ValueError("test_health_webhook cannot be combined with another mode")
        return test_health_webhook()
    if event.get("test_webhooks"):
        if event.get("seed_only") or backfill_category:
            raise ValueError("test_webhooks cannot be combined with another mode")
        return test_webhooks()
    seed_only = bool(event.get("seed_only", False))
    if seed_only and backfill_category:
        raise ValueError("seed_only and backfill_category cannot both be set")
    if backfill_category and backfill_category not in {category.key for category in CATEGORIES}:
        raise ValueError(f"Unknown backfill category: {backfill_category}")
    requested = event.get("seed_sources") or []
    if isinstance(requested, str):
        requested = [requested]
    if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
        raise ValueError("seed_sources must be a source name or a list of source names")
    if requested and not seed_only:
        raise ValueError("seed_sources requires seed_only=true")
    if requested and backfill_category:
        raise ValueError("seed_sources and backfill_category cannot both be set")
    partition_index = event.get("seed_partition")
    partition_count = event.get("seed_partition_count")
    if (partition_index is None) != (partition_count is None):
        raise ValueError("seed_partition and seed_partition_count must be supplied together")
    seed_partition = None
    if partition_index is not None:
        if not seed_only and not backfill_category:
            raise ValueError("seed_partition requires seed_only=true or backfill_category")
        if requested:
            raise ValueError("use either seed_sources or seed_partition, not both")
        if isinstance(partition_index, bool) or isinstance(partition_count, bool):
            raise ValueError("seed partition values must be integers")
        try:
            partition_index = int(partition_index)
            partition_count = int(partition_count)
        except (TypeError, ValueError) as error:
            raise ValueError("seed partition values must be integers") from error
        if partition_count < 1 or partition_count > 100:
            raise ValueError("seed_partition_count must be between 1 and 100")
        if partition_index < 0 or partition_index >= partition_count:
            raise ValueError("seed_partition must be from 0 through seed_partition_count - 1")
        seed_partition = (partition_index, partition_count)
    only_sources = {name.strip() for name in requested if name.strip()} or None
    return execute(
        seed_only=seed_only,
        only_sources=only_sources,
        seed_partition=seed_partition,
        backfill_category=backfill_category,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch internship postings and route new ones to Discord.")
    parser.add_argument("--seed-only", action="store_true", help="record current matches without notifying Discord")
    parser.add_argument(
        "--backfill-category",
        choices=[category.key for category in CATEGORIES],
        help="deliver known-dated jobs from the last MAX_POSTING_AGE_DAYS",
    )
    args = parser.parse_args()
    execute(seed_only=args.seed_only, backfill_category=args.backfill_category)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

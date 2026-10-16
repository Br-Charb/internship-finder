"""Shared Discord notification-role helpers.

The scheduled scraper only reads role records from DynamoDB.  The separate
Discord interaction Lambda creates roles and changes subscriptions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_TIMEOUT_SECONDS = 15
NON_COMPANY_SOURCE_SECTIONS = {"getro", "list", "usajobs"}

CATEGORY_CHANNEL_ENV = {
    "software": "DISCORD_CHANNEL_SOFTWARE",
    "hardware": "DISCORD_CHANNEL_HARDWARE",
    "mechanical": "DISCORD_CHANNEL_MECHANICAL",
    "aerospace": "DISCORD_CHANNEL_AEROSPACE",
    "accounting": "DISCORD_CHANNEL_ACCOUNTING",
    "finance": "DISCORD_CHANNEL_FINANCE",
}

CATEGORY_SHORT_LABELS = {
    "software": "Software",
    "hardware": "Hardware",
    "mechanical": "Mechanical",
    "aerospace": "Aerospace",
    "accounting": "Accounting",
    "finance": "Finance",
}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def normalize_company(value: str) -> str:
    """Return a stable, human-name identity used by commands and job delivery."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def notification_role_key(guild_id: str, category: str, company: str) -> str:
    identity = normalize_company(company)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"__notify_role__:{guild_id}:{category}:{digest}"


def notification_user_key(guild_id: str, user_id: str) -> str:
    return f"__notify_user__:{guild_id}:{user_id}"


def subscription_token(category: str, company: str) -> str:
    return json.dumps([category, company], ensure_ascii=False, separators=(",", ":"))


def parse_subscription_token(value: str) -> tuple[str, str] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) for item in decoded)
    ):
        return None
    return decoded[0], decoded[1]


def load_company_names(sources_file: Path) -> list[str]:
    """Load canonical direct-company names from the deployed source catalog."""
    config = tomllib.loads(sources_file.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for section, entries in config.items():
        if section in NON_COMPANY_SOURCE_SECTIONS or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            identity = normalize_company(name)
            if name and identity:
                names.setdefault(identity, name)
    return sorted(names.values(), key=str.casefold)


def resolve_company(value: str, companies: list[str]) -> str | None:
    """Resolve an exact name, or a unique prefix entered without autocomplete."""
    wanted = normalize_company(value)
    if not wanted:
        return None
    by_identity = {normalize_company(company): company for company in companies}
    if wanted in by_identity:
        return by_identity[wanted]
    prefix_matches = [
        company for identity, company in by_identity.items()
        if identity.startswith(wanted)
    ]
    return prefix_matches[0] if len(prefix_matches) == 1 else None


def autocomplete_companies(value: str, companies: list[str], limit: int = 25) -> list[str]:
    wanted = normalize_company(value)
    ranked: list[tuple[int, str]] = []
    for company in companies:
        identity = normalize_company(company)
        if not wanted:
            rank = 0
        elif identity.startswith(wanted):
            rank = 0
        elif wanted in identity:
            rank = 1
        else:
            continue
        ranked.append((rank, company))
    ranked.sort(key=lambda item: (item[0], item[1].casefold()))
    return [company for _rank, company in ranked[:limit]]


def category_channels_from_env() -> dict[str, str]:
    channels: dict[str, str] = {}
    for category, variable in CATEGORY_CHANNEL_ENV.items():
        value = os.environ.get(variable, "").strip()
        if value:
            if not value.isdigit():
                raise ValueError(f"{variable} must be a numeric Discord channel ID")
            channels[category] = value
    return channels


class DiscordAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Discord API returned HTTP {status}: {message[:300]}")
        self.status = status


def discord_request(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    bot_token: str | None = None,
    expected: set[int] | None = None,
    attempts: int = 4,
) -> dict[str, Any] | None:
    """Call a Discord REST endpoint with bounded rate-limit/server retries."""
    expected = expected or {200, 201, 204}
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": "internship-finder-discord/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if bot_token:
        headers["Authorization"] = f"Bot {bot_token}"

    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=DISCORD_TIMEOUT_SECONDS) as response:
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            response_body = error.read()
        except urllib.error.URLError as error:
            if attempt == attempts - 1:
                raise RuntimeError(f"Discord API network error: {error.reason}") from error
            time.sleep(2 ** attempt)
            continue

        if status in expected:
            if not response_body:
                return None
            try:
                decoded = json.loads(response_body)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, dict) else None

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

        message = response_body.decode("utf-8", errors="replace") if response_body else ""
        raise DiscordAPIError(status, message)
    raise RuntimeError("Discord API retries exhausted")


class DiscordBotClient:
    """Small Discord REST client; no persistent gateway connection is needed."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("DISCORD_BOT_TOKEN cannot be empty")

    @staticmethod
    def _snowflake(value: str, label: str) -> str:
        value = str(value).strip()
        if not value.isdigit():
            raise ValueError(f"{label} must be a numeric Discord ID")
        return value

    def create_role(self, guild_id: str, name: str) -> dict[str, Any]:
        guild_id = self._snowflake(guild_id, "guild_id")
        result = discord_request(
            f"{DISCORD_API_BASE}/guilds/{guild_id}/roles",
            method="POST",
            payload={
                "name": name[:100],
                "permissions": "0",
                "color": 0,
                "hoist": False,
                # Keeping this false prevents ordinary members from pinging the
                # cohort. The bot uses MENTION_EVERYONE for the controlled ping.
                "mentionable": False,
            },
            bot_token=self.token,
            expected={200},
        )
        if not result or not str(result.get("id") or "").isdigit():
            raise RuntimeError("Discord did not return the created role ID")
        return result

    def delete_role(self, guild_id: str, role_id: str) -> None:
        guild_id = self._snowflake(guild_id, "guild_id")
        role_id = self._snowflake(role_id, "role_id")
        discord_request(
            f"{DISCORD_API_BASE}/guilds/{guild_id}/roles/{role_id}",
            method="DELETE",
            bot_token=self.token,
            expected={204},
        )

    def add_member_role(self, guild_id: str, user_id: str, role_id: str) -> None:
        guild_id = self._snowflake(guild_id, "guild_id")
        user_id = self._snowflake(user_id, "user_id")
        role_id = self._snowflake(role_id, "role_id")
        discord_request(
            f"{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            method="PUT",
            bot_token=self.token,
            expected={204},
        )

    def remove_member_role(self, guild_id: str, user_id: str, role_id: str) -> None:
        guild_id = self._snowflake(guild_id, "guild_id")
        user_id = self._snowflake(user_id, "user_id")
        role_id = self._snowflake(role_id, "role_id")
        discord_request(
            f"{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            method="DELETE",
            bot_token=self.token,
            expected={204},
        )

    def create_message(self, channel_id: str, payload: dict[str, Any]) -> None:
        channel_id = self._snowflake(channel_id, "channel_id")
        discord_request(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            method="POST",
            payload=payload,
            bot_token=self.token,
            expected={200},
        )


class DynamoNotificationStore:
    """Subscription records stored alongside the scraper's seen-job records."""

    def __init__(self, table_name: str) -> None:
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - Lambda supplies boto3
            raise RuntimeError("boto3 is required outside Lambda") from error
        self.table = boto3.resource("dynamodb").Table(table_name)

    def get_role(self, guild_id: str, category: str, company: str) -> dict[str, Any] | None:
        response = self.table.get_item(
            Key={"job_id": notification_role_key(guild_id, category, company)},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return item if isinstance(item, dict) else None

    def create_role_record(
        self,
        guild_id: str,
        category: str,
        company: str,
        role_id: str,
    ) -> bool:
        now = utc_now_iso()
        try:
            self.table.put_item(
                Item={
                    "job_id": notification_role_key(guild_id, category, company),
                    "state": "notification_role",
                    "guild_id": guild_id,
                    "category": category,
                    "company": company,
                    "company_key": normalize_company(company),
                    "role_id": role_id,
                    "created_at": now,
                    "updated_at": now,
                },
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except Exception as error:
            if conditional_failure(error):
                return False
            raise
        return True

    def add_subscriber(
        self,
        guild_id: str,
        category: str,
        company: str,
        user_id: str,
    ) -> bool:
        try:
            self.table.update_item(
                Key={"job_id": notification_role_key(guild_id, category, company)},
                UpdateExpression="SET updated_at = :now ADD subscribers :subscribers",
                ConditionExpression=(
                    "attribute_exists(job_id) AND "
                    "(attribute_not_exists(subscribers) OR NOT contains(subscribers, :user_id))"
                ),
                ExpressionAttributeValues={
                    ":now": utc_now_iso(),
                    ":subscribers": {user_id},
                    ":user_id": user_id,
                },
            )
        except Exception as error:
            if conditional_failure(error):
                return False
            raise
        return True

    def remove_subscriber(
        self,
        guild_id: str,
        category: str,
        company: str,
        user_id: str,
    ) -> tuple[bool, bool]:
        try:
            response = self.table.update_item(
                Key={"job_id": notification_role_key(guild_id, category, company)},
                UpdateExpression="SET updated_at = :now DELETE subscribers :subscribers",
                ConditionExpression="contains(subscribers, :user_id)",
                ExpressionAttributeValues={
                    ":now": utc_now_iso(),
                    ":subscribers": {user_id},
                    ":user_id": user_id,
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if conditional_failure(error):
                return False, False
            raise
        remaining = response.get("Attributes", {}).get("subscribers") or set()
        return True, not remaining

    def delete_role_record_if_empty(
        self,
        guild_id: str,
        category: str,
        company: str,
        role_id: str,
    ) -> bool:
        try:
            self.table.delete_item(
                Key={"job_id": notification_role_key(guild_id, category, company)},
                ConditionExpression=(
                    "role_id = :role_id AND attribute_not_exists(subscribers)"
                ),
                ExpressionAttributeValues={":role_id": role_id},
            )
        except Exception as error:
            if conditional_failure(error):
                return False
            raise
        return True

    def add_user_subscription(
        self,
        guild_id: str,
        user_id: str,
        category: str,
        company: str,
    ) -> None:
        self.table.update_item(
            Key={"job_id": notification_user_key(guild_id, user_id)},
            UpdateExpression=(
                "SET #state = :state, guild_id = :guild_id, user_id = :user_id, "
                "updated_at = :now ADD subscriptions :subscriptions"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":state": "notification_user",
                ":guild_id": guild_id,
                ":user_id": user_id,
                ":now": utc_now_iso(),
                ":subscriptions": {subscription_token(category, company)},
            },
        )

    def remove_user_subscription(
        self,
        guild_id: str,
        user_id: str,
        category: str,
        company: str,
    ) -> None:
        try:
            self.table.update_item(
                Key={"job_id": notification_user_key(guild_id, user_id)},
                UpdateExpression="SET updated_at = :now DELETE subscriptions :subscriptions",
                ConditionExpression="attribute_exists(job_id)",
                ExpressionAttributeValues={
                    ":now": utc_now_iso(),
                    ":subscriptions": {subscription_token(category, company)},
                },
            )
        except Exception as error:
            if not conditional_failure(error):
                raise

    def user_subscriptions(self, guild_id: str, user_id: str) -> list[tuple[str, str]]:
        response = self.table.get_item(
            Key={"job_id": notification_user_key(guild_id, user_id)},
            ConsistentRead=True,
        )
        item = response.get("Item") or {}
        parsed = [
            result
            for value in item.get("subscriptions") or set()
            if (result := parse_subscription_token(value)) is not None
        ]
        return sorted(parsed, key=lambda row: (row[0], row[1].casefold()))

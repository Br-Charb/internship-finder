"""AWS Lambda handler for Discord notification slash commands.

Deploy this file as a second Lambda with handler
``discord_commands.lambda_handler`` and a public Function URL. Discord sends
signed interaction webhooks to that URL. Command work is self-invoked
asynchronously so the public request can meet Discord's three-second deadline.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from jobfinder.notifications import (
    CATEGORY_SHORT_LABELS,
    DISCORD_API_BASE,
    DiscordAPIError,
    DiscordBotClient,
    DynamoNotificationStore,
    autocomplete_companies,
    category_channels_from_env,
    discord_request,
    load_company_names,
    resolve_company,
)


ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.toml"
DEFAULT_TABLE = "internship-finder-seen"
EPHEMERAL_FLAG = 1 << 6
SIGNATURE_MAX_AGE_SECONDS = 5 * 60


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _interaction_message(content: str) -> dict[str, Any]:
    return {
        "type": 4,
        "data": {
            "content": content[:2000],
            "flags": EPHEMERAL_FLAG,
            "allowed_mentions": {"parse": []},
        },
    }


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (event.get("headers") or {}).items()
    }


def _raw_body(event: dict[str, Any]) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return str(body)


def verify_discord_signature(event: dict[str, Any]) -> bool:
    """Validate Discord's Ed25519 signature and reject stale replays."""
    headers = _headers(event)
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    public_key = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()
    if not signature or not timestamp or not public_key:
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        return False
    try:
        timestamp_value = int(timestamp)
        if abs(int(time.time()) - timestamp_value) > SIGNATURE_MAX_AGE_SECONDS:
            return False
        VerifyKey(bytes.fromhex(public_key)).verify(
            (timestamp + _raw_body(event)).encode("utf-8"),
            bytes.fromhex(signature),
        )
    except (BadSignatureError, ValueError, TypeError):
        return False
    return True


def _company_option(interaction: dict[str, Any]) -> str:
    for option in interaction.get("data", {}).get("options") or []:
        if option.get("name") == "company":
            return str(option.get("value") or "").strip()
    return ""


def _category_for_interaction(interaction: dict[str, Any]) -> str | None:
    channel_id = str(interaction.get("channel_id") or "")
    for category, configured_channel in category_channels_from_env().items():
        if configured_channel == channel_id:
            return category
    return None


def _user_id(interaction: dict[str, Any]) -> str:
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return str(user.get("id") or "")


def _role_name(company: str, category: str) -> str:
    label = CATEGORY_SHORT_LABELS.get(category, category.title())
    return f"Notify: {company} / {label}"[:100]


def _ensure_role(
    store: Any,
    bot: Any,
    guild_id: str,
    category: str,
    company: str,
) -> tuple[str, bool]:
    """Return the cohort role ID, resolving simultaneous first subscriptions."""
    record = store.get_role(guild_id, category, company)
    if record and str(record.get("role_id") or "").isdigit():
        return str(record["role_id"]), False

    created = bot.create_role(guild_id, _role_name(company, category))
    role_id = str(created["id"])
    if store.create_role_record(guild_id, category, company, role_id):
        return role_id, True

    # Another invocation won the conditional write. Delete our duplicate and
    # use the role recorded by the winner.
    try:
        bot.delete_role(guild_id, role_id)
    except DiscordAPIError as error:
        if error.status != 404:
            raise
    winner = store.get_role(guild_id, category, company)
    winner_id = str((winner or {}).get("role_id") or "")
    if not winner_id.isdigit():
        raise RuntimeError("Notification role creation raced but no winning role was found")
    return winner_id, False


def _subscribe(
    store: Any,
    bot: Any,
    guild_id: str,
    user_id: str,
    category: str,
    company: str,
) -> str:
    role_id, created_role = _ensure_role(store, bot, guild_id, category, company)
    try:
        bot.add_member_role(guild_id, user_id, role_id)
        added = store.add_subscriber(guild_id, category, company, user_id)
        store.add_user_subscription(guild_id, user_id, category, company)
    except Exception:
        # Avoid leaving a brand-new empty role after a partial first subscribe.
        if created_role:
            try:
                if store.delete_role_record_if_empty(
                    guild_id, category, company, role_id
                ):
                    bot.delete_role(guild_id, role_id)
            except Exception:
                pass
        raise

    channel = CATEGORY_SHORT_LABELS.get(category, category.title())
    if added:
        return f"Subscribed to **{company}** in the **{channel}** channel."
    return f"You were already subscribed to **{company}** in **{channel}**."


def _unsubscribe(
    store: Any,
    bot: Any,
    guild_id: str,
    user_id: str,
    category: str,
    company: str,
) -> str:
    record = store.get_role(guild_id, category, company)
    role_id = str((record or {}).get("role_id") or "")
    channel = CATEGORY_SHORT_LABELS.get(category, category.title())
    subscribers = set((record or {}).get("subscribers") or set())
    if not role_id.isdigit() or user_id not in subscribers:
        # Repair a stale reverse index without treating it as a command failure.
        store.remove_user_subscription(guild_id, user_id, category, company)
        return f"You were not subscribed to **{company}** in **{channel}**."

    try:
        bot.remove_member_role(guild_id, user_id, role_id)
    except DiscordAPIError as error:
        if error.status != 404:
            raise

    removed, empty = store.remove_subscriber(
        guild_id, category, company, user_id
    )
    store.remove_user_subscription(guild_id, user_id, category, company)
    if not removed:
        return f"You were not subscribed to **{company}** in **{channel}**."

    if empty and store.delete_role_record_if_empty(
        guild_id, category, company, role_id
    ):
        try:
            bot.delete_role(guild_id, role_id)
        except DiscordAPIError as error:
            if error.status != 404:
                raise

    return f"Unsubscribed from **{company}** in the **{channel}** channel."


def execute_command(interaction: dict[str, Any], store: Any, bot: Any) -> str:
    """Execute one validated application command and return its private reply."""
    guild_id = str(interaction.get("guild_id") or "")
    expected_guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not guild_id or not expected_guild or guild_id != expected_guild:
        return "This command is only available in the configured Internship Finder server."

    category = _category_for_interaction(interaction)
    if category is None:
        return "Use this command inside one of the six configured job-alert channels."

    user_id = _user_id(interaction)
    if not user_id.isdigit():
        return "Discord did not provide a valid server member for this command."

    command = str(interaction.get("data", {}).get("name") or "")
    if command == "notifications":
        subscriptions = [
            company
            for saved_category, company in store.user_subscriptions(guild_id, user_id)
            if saved_category == category
        ]
        label = CATEGORY_SHORT_LABELS.get(category, category.title())
        if not subscriptions:
            return f"You have no company notifications in **{label}**."
        lines = "\n".join(f"- {company}" for company in subscriptions)
        return f"Your **{label}** company notifications:\n{lines}"[:2000]

    if command not in {"notify", "unnotify"}:
        return "Unknown command."

    companies = load_company_names(SOURCES_FILE)
    entered = _company_option(interaction)
    company = resolve_company(entered, companies)
    if company is None:
        return (
            "I couldn't find one unambiguous company with that name. "
            "Run the command again and select a company from autocomplete."
        )

    if command == "notify":
        return _subscribe(store, bot, guild_id, user_id, category, company)
    return _unsubscribe(store, bot, guild_id, user_id, category, company)


def _autocomplete(interaction: dict[str, Any]) -> dict[str, Any]:
    if _category_for_interaction(interaction) is None:
        return {"type": 8, "data": {"choices": []}}
    current = _company_option(interaction)
    choices = autocomplete_companies(current, load_company_names(SOURCES_FILE))
    return {
        "type": 8,
        "data": {
            "choices": [{"name": company[:100], "value": company[:100]} for company in choices]
        },
    }


def _invoke_worker(interaction: dict[str, Any]) -> None:
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is unavailable")
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - Lambda supplies boto3
        raise RuntimeError("boto3 is required outside Lambda") from error
    response = boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"_discord_command_worker": True, "interaction": interaction}).encode(
            "utf-8"
        ),
    )
    if int(response.get("StatusCode") or 0) != 202:
        raise RuntimeError("Lambda did not accept the asynchronous command worker")


def _edit_original_response(interaction: dict[str, Any], content: str) -> None:
    application_id = str(interaction.get("application_id") or "")
    token = str(interaction.get("token") or "")
    if not application_id.isdigit() or not token:
        raise ValueError("Interaction is missing its application ID or response token")
    encoded_token = urllib.parse.quote(token, safe="")
    discord_request(
        f"{DISCORD_API_BASE}/webhooks/{application_id}/{encoded_token}/messages/@original",
        method="PATCH",
        payload={"content": content[:2000], "allowed_mentions": {"parse": []}},
        expected={200},
    )


def _run_worker(event: dict[str, Any]) -> dict[str, Any]:
    interaction = event.get("interaction") or {}
    table_name = os.environ.get("DYNAMODB_TABLE", DEFAULT_TABLE).strip()
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    try:
        if not table_name or not bot_token:
            raise ValueError("DYNAMODB_TABLE and DISCORD_BOT_TOKEN must be configured")
        content = execute_command(
            interaction,
            DynamoNotificationStore(table_name),
            DiscordBotClient(bot_token),
        )
        outcome = "completed"
    except Exception as error:
        # Never put the bot token or interaction token in logs.
        print(f"ERROR Discord command worker: {type(error).__name__}: {error}")
        content = "I couldn't update that notification right now. Please try again shortly."
        outcome = "failed"
    _edit_original_response(interaction, content)
    return {"discord_command_worker": outcome}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    if event.get("_discord_command_worker"):
        return _run_worker(event)

    if not verify_discord_signature(event):
        return _response(401, {"error": "invalid request signature"})
    try:
        interaction = json.loads(_raw_body(event))
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON"})

    interaction_type = interaction.get("type")
    if interaction_type == 1:
        return _response(200, {"type": 1})
    if interaction_type == 4:
        return _response(200, _autocomplete(interaction))
    if interaction_type != 2:
        return _response(200, _interaction_message("Unsupported Discord interaction."))

    try:
        _invoke_worker(interaction)
    except Exception as error:
        print(f"ERROR starting Discord command worker: {type(error).__name__}: {error}")
        return _response(
            200,
            _interaction_message(
                "I couldn't start that notification update. Please try again shortly."
            ),
        )
    return _response(
        200,
        {"type": 5, "data": {"flags": EPHEMERAL_FLAG}},
    )

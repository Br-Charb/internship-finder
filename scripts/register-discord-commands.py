"""Register the notification commands in one Discord server.

Required environment variables:
  DISCORD_APPLICATION_ID, DISCORD_GUILD_ID

DISCORD_BOT_TOKEN is optional; when omitted, the script prompts without echoing.
"""
from __future__ import annotations

import getpass
import json
import os
import urllib.error
import urllib.request


DISCORD_API_BASE = "https://discord.com/api/v10"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    application_id = required("DISCORD_APPLICATION_ID")
    guild_id = required("DISCORD_GUILD_ID")
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token:
        bot_token = getpass.getpass("Discord bot token (input hidden): ").strip()
    if not bot_token:
        raise SystemExit("A Discord bot token is required")
    if not application_id.isdigit() or not guild_id.isdigit():
        raise SystemExit("DISCORD_APPLICATION_ID and DISCORD_GUILD_ID must be numeric IDs")

    company_option = {
        "type": 3,
        "name": "company",
        "description": "Company to follow in this job channel",
        "required": True,
        "autocomplete": True,
    }
    commands = [
        {
            "name": "notify",
            "description": "Notify me when this company posts in this job channel",
            "type": 1,
            "options": [company_option],
        },
        {
            "name": "unnotify",
            "description": "Stop notifications for a company in this job channel",
            "type": 1,
            "options": [company_option],
        },
        {
            "name": "notifications",
            "description": "List my company notifications for this job channel",
            "type": 1,
        },
    ]
    url = f"{DISCORD_API_BASE}/applications/{application_id}/guilds/{guild_id}/commands"
    request = urllib.request.Request(
        url,
        data=json.dumps(commands).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "internship-finder-command-registration/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            registered = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Discord returned HTTP {error.code}: {detail[:500]}") from error
    names = ", ".join(f"/{command['name']}" for command in registered)
    print(f"Registered {len(registered)} server commands: {names}")


if __name__ == "__main__":
    main()

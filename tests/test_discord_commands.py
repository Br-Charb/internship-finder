import json
import os
import unittest
from unittest.mock import patch

import discord_commands
from jobfinder.notifications import (
    autocomplete_companies,
    normalize_company,
    resolve_company,
)


class FakeStore:
    def __init__(self):
        self.roles = {}
        self.users = {}

    @staticmethod
    def key(guild_id, category, company):
        return guild_id, category, normalize_company(company)

    def get_role(self, guild_id, category, company):
        record = self.roles.get(self.key(guild_id, category, company))
        if record is None:
            return None
        return {**record, "subscribers": set(record.get("subscribers", set()))}

    def create_role_record(self, guild_id, category, company, role_id):
        key = self.key(guild_id, category, company)
        if key in self.roles:
            return False
        self.roles[key] = {
            "role_id": role_id,
            "company": company,
            "subscribers": set(),
        }
        return True

    def add_subscriber(self, guild_id, category, company, user_id):
        subscribers = self.roles[self.key(guild_id, category, company)]["subscribers"]
        if user_id in subscribers:
            return False
        subscribers.add(user_id)
        return True

    def remove_subscriber(self, guild_id, category, company, user_id):
        subscribers = self.roles[self.key(guild_id, category, company)]["subscribers"]
        if user_id not in subscribers:
            return False, False
        subscribers.remove(user_id)
        return True, not subscribers

    def delete_role_record_if_empty(self, guild_id, category, company, role_id):
        key = self.key(guild_id, category, company)
        record = self.roles.get(key)
        if not record or record["role_id"] != role_id or record["subscribers"]:
            return False
        del self.roles[key]
        return True

    def add_user_subscription(self, guild_id, user_id, category, company):
        self.users.setdefault((guild_id, user_id), set()).add((category, company))

    def remove_user_subscription(self, guild_id, user_id, category, company):
        self.users.setdefault((guild_id, user_id), set()).discard((category, company))

    def user_subscriptions(self, guild_id, user_id):
        return sorted(self.users.get((guild_id, user_id), set()))


class FakeBot:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.added = []
        self.removed = []

    def create_role(self, guild_id, name):
        role_id = str(1000 + len(self.created))
        self.created.append((guild_id, name, role_id))
        return {"id": role_id}

    def delete_role(self, guild_id, role_id):
        self.deleted.append((guild_id, role_id))

    def add_member_role(self, guild_id, user_id, role_id):
        self.added.append((guild_id, user_id, role_id))

    def remove_member_role(self, guild_id, user_id, role_id):
        self.removed.append((guild_id, user_id, role_id))


def interaction(command="notify", channel_id="10", company="Meta"):
    options = [] if command == "notifications" else [
        {"name": "company", "type": 3, "value": company}
    ]
    return {
        "id": "999",
        "application_id": "888",
        "token": "interaction-token",
        "type": 2,
        "guild_id": "123",
        "channel_id": channel_id,
        "member": {"user": {"id": "55"}},
        "data": {"name": command, "options": options},
    }


class NotificationCommandTests(unittest.TestCase):
    def env(self):
        return patch.dict(
            os.environ,
            {
                "DISCORD_GUILD_ID": "123",
                "DISCORD_CHANNEL_SOFTWARE": "10",
                "DISCORD_CHANNEL_ACCOUNTING": "20",
            },
            clear=False,
        )

    def test_company_matching_and_autocomplete(self):
        companies = ["Meta", "Metagenomi", "Microsoft"]
        self.assertEqual("meta", normalize_company("META"))
        self.assertEqual("Meta", resolve_company("meta", companies))
        self.assertIsNone(resolve_company("met", companies))
        self.assertEqual(["Microsoft"], autocomplete_companies("soft", companies))

    def test_subscription_is_scoped_to_current_category_channel(self):
        store = FakeStore()
        bot = FakeBot()
        with self.env():
            first = discord_commands.execute_command(interaction(), store, bot)
            repeat = discord_commands.execute_command(interaction(), store, bot)
            accounting = discord_commands.execute_command(
                interaction(channel_id="20"), store, bot
            )

        self.assertIn("Subscribed", first)
        self.assertIn("already subscribed", repeat)
        self.assertIn("Accounting", accounting)
        self.assertEqual(2, len(bot.created))
        self.assertEqual(2, len(store.roles))

    def test_notifications_lists_only_current_channel(self):
        store = FakeStore()
        bot = FakeBot()
        with self.env():
            discord_commands.execute_command(interaction(), store, bot)
            discord_commands.execute_command(
                interaction(channel_id="20", company="Deloitte"), store, bot
            )
            result = discord_commands.execute_command(
                interaction(command="notifications"), store, bot
            )
        self.assertIn("Meta", result)
        self.assertNotIn("Deloitte", result)

    def test_last_unsubscribe_deletes_empty_role(self):
        store = FakeStore()
        bot = FakeBot()
        with self.env():
            discord_commands.execute_command(interaction(), store, bot)
            result = discord_commands.execute_command(
                interaction(command="unnotify"), store, bot
            )
        self.assertIn("Unsubscribed", result)
        self.assertEqual(1, len(bot.deleted))
        self.assertEqual({}, store.roles)

    def test_command_outside_configured_channels_is_rejected(self):
        with self.env():
            result = discord_commands.execute_command(
                interaction(channel_id="9999"), FakeStore(), FakeBot()
            )
        self.assertIn("six configured", result)

    def test_ping_interaction_response(self):
        event = {"body": json.dumps({"type": 1}), "headers": {}}
        with patch.object(discord_commands, "verify_discord_signature", return_value=True):
            response = discord_commands.lambda_handler(event, None)
        self.assertEqual(200, response["statusCode"])
        self.assertEqual({"type": 1}, json.loads(response["body"]))

    def test_command_defers_and_starts_async_worker(self):
        event = {"body": json.dumps(interaction()), "headers": {}}
        with patch.object(
            discord_commands, "verify_discord_signature", return_value=True
        ), patch.object(discord_commands, "_invoke_worker") as invoke:
            response = discord_commands.lambda_handler(event, None)
        self.assertEqual(5, json.loads(response["body"])["type"])
        invoke.assert_called_once()

    def test_autocomplete_returns_meta(self):
        autocomplete_interaction = interaction()
        autocomplete_interaction["type"] = 4
        autocomplete_interaction["data"]["options"][0]["value"] = "met"
        event = {"body": json.dumps(autocomplete_interaction), "headers": {}}
        with self.env(), patch.object(
            discord_commands, "verify_discord_signature", return_value=True
        ):
            response = discord_commands.lambda_handler(event, None)
        choices = json.loads(response["body"])["data"]["choices"]
        self.assertTrue(any(choice["name"] == "Meta" for choice in choices))
        self.assertLessEqual(len(choices), 25)


if __name__ == "__main__":
    unittest.main()

import os
import datetime as dt
import unittest
from unittest.mock import patch

import run


JOBS = [
    {
        "company": "Example Co",
        "title": "Software Engineering Intern",
        "location": "Remote",
        "url": "https://jobs.example.test/software-intern-123",
        "source": "Example careers",
        "posted": "2026-10-13",
        "description": "Qualifications:\n- Python experience\n- React experience",
    }
]


class FakeState:
    instance = None

    def __init__(self, table_name):
        self.table_name = table_name
        self.seeded = []
        self.claimed = []
        self.completed = []
        self.released = []
        self.saved_revisions = []
        FakeState.instance = self

    def known_ids(self, job_ids):
        return set()

    def source_revisions(self, source_names):
        return {}

    def save_source_revision(self, source_name, revision):
        self.saved_revisions.append((source_name, revision))

    def seed(self, job, category):
        self.seeded.append((job["key"], category.key))
        return True

    def claim(self, job, category):
        self.claimed.append((job["key"], category.key))
        return "claim-token"

    def complete(self, job_id, token, state="delivered"):
        self.completed.append((job_id, token, state))

    def release(self, job_id, token):
        self.released.append((job_id, token))


class ExecutionTests(unittest.TestCase):
    def env(self):
        return patch.dict(
            os.environ,
            {
                "MAX_DESCRIPTION_LOOKUPS": "0",
                "MAX_JOBS_PER_RUN": "40",
                "DYNAMODB_TABLE": "test-seen",
            },
            clear=False,
        )

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=(JOBS, []))
    def test_seed_records_without_webhook(self, _fetch, _revision):
        with self.env(), patch.object(run, "_webhook_urls", side_effect=AssertionError("webhooks not needed")):
            summary = run.execute(seed_only=True)
        self.assertEqual(1, summary["seeded"])
        self.assertEqual("software", FakeState.instance.seeded[0][1])

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=(JOBS, []))
    def test_delivery_completes_claim(self, _fetch, _revision):
        urls = {category.key: f"https://discord.com/api/webhooks/{category.key}/secret" for category in run.CATEGORIES}
        with self.env(), patch.object(run, "_webhook_urls", return_value=urls), \
                patch.object(run, "post_to_discord") as post:
            summary = run.execute()
        self.assertEqual(1, summary["delivered"])
        self.assertEqual("software", FakeState.instance.claimed[0][1])
        self.assertEqual("delivered", FakeState.instance.completed[0][2])
        post.assert_called_once()

    def test_polling_tiers(self):
        # Epoch minute 0 is divisible by both 15 and 60 minutes.
        top_of_hour = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual({1, 2, 3}, run._active_tiers(top_of_hour))
        self.assertEqual({1}, run._active_tiers(top_of_hour + dt.timedelta(minutes=5)))
        self.assertEqual({1, 2}, run._active_tiers(top_of_hour + dt.timedelta(minutes=15)))

    def test_discord_embed_layout_and_graduation(self):
        job = {
            **JOBS[0],
            "description": (
                "Applicants should graduate between December 2026 and May 2027.\n"
                "Qualifications:\n- Python experience\n- React experience"
            ),
        }
        category = next(item for item in run.CATEGORIES if item.key == "software")
        payload = run.discord_payload(job, category, ["software", "python"])
        embed = payload["embeds"][0]
        self.assertEqual(job["title"], embed["title"])
        self.assertEqual(job["url"], embed["url"])
        fields = {field["name"]: field for field in embed["fields"]}
        self.assertEqual("Example Co", fields["Company"]["value"])
        self.assertEqual("Dec 2026 – May 2027", fields["Graduation"]["value"])
        self.assertTrue(fields["Company"]["inline"])
        self.assertTrue(fields["Graduation"]["inline"])

    @patch.object(run, "github_list_revision", return_value="same-sha")
    def test_unchanged_github_list_is_skipped(self, _revision):
        class RevisionState:
            def source_revisions(self, _names):
                return {"Large list": "same-sha"}

        config = {"list": [{"name": "Large list", "urls": ["https://example.test/jobs.json"]}]}
        filtered, pending, cached = run._skip_unchanged_lists(config, RevisionState())
        self.assertEqual([], filtered["list"])
        self.assertEqual({}, pending)
        self.assertEqual(["Large list"], cached)
        self.assertEqual(1, len(config["list"]))  # input was not mutated


if __name__ == "__main__":
    unittest.main()

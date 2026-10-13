import os
import datetime as dt
import unittest
from unittest.mock import patch

import run


JOBS = [
    {
        "company": "Example Co",
        "title": "Software Engineering Intern",
        "location": "Remote - US",
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
        self.ignored = []
        self.saved_revisions = []
        FakeState.instance = self

    def known_ids(self, job_ids):
        return set()

    def source_revisions(self, source_names):
        return {}

    def save_source_revision(self, source_name, revision):
        self.saved_revisions.append((source_name, revision))

    def seed(self, job, category):
        self.seeded.append((job["key"], category.key if category else "uncategorized"))
        return True

    def ignore(self, job, reason="uncategorized"):
        self.ignored.append((job["key"], reason))
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
        with self.env(), patch.object(run, "_webhook_urls", side_effect=AssertionError("webhooks not needed")), \
                patch.object(run, "_fill_descriptions", side_effect=AssertionError("seed should not fetch descriptions")):
            summary = run.execute(seed_only=True)
        self.assertEqual(1, summary["seeded"])
        self.assertEqual("software", FakeState.instance.seeded[0][1])

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=([{
        "company": "Example Co",
        "title": "General Intern",
        "location": "Boston, MA",
        "url": "https://jobs.example.test/general-intern-seed",
        "source": "Example careers",
        "posted": "2026-10-13",
        "description": "Assist with general projects.",
    }], []))
    def test_seed_records_uncategorized_baseline(self, _fetch, _revision):
        with self.env():
            summary = run.execute(seed_only=True)
        self.assertEqual(1, summary["seeded"])
        self.assertEqual(1, summary["ignored"])
        self.assertEqual("uncategorized", FakeState.instance.seeded[0][1])

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

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=([{
        "company": "Example Co",
        "title": "General Intern",
        "location": "Boston, MA",
        "url": "https://jobs.example.test/general-intern-456",
        "source": "Example careers",
        "posted": "2026-10-13",
        "description": "Assist with general projects.",
    }], []))
    def test_uncategorized_job_is_remembered(self, _fetch, _revision):
        urls = {category.key: f"https://discord.com/api/webhooks/{category.key}/secret" for category in run.CATEGORIES}
        with self.env(), patch.object(run, "_webhook_urls", return_value=urls), \
                patch.object(run, "post_to_discord") as post:
            summary = run.execute()
        self.assertEqual(1, summary["ignored"])
        self.assertEqual(1, len(FakeState.instance.ignored))
        self.assertEqual("uncategorized", FakeState.instance.ignored[0][1])
        post.assert_not_called()

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=([{
        **JOBS[0],
        "location": "Toronto, Canada",
        "url": "https://jobs.example.test/canada-intern-1",
    }], []))
    def test_non_us_job_is_remembered_without_delivery(self, _fetch, _revision):
        urls = {category.key: f"https://discord.com/api/webhooks/{category.key}/secret" for category in run.CATEGORIES}
        with self.env(), patch.object(run, "_webhook_urls", return_value=urls), \
                patch.object(run, "post_to_discord") as post:
            summary = run.execute()
        self.assertEqual(1, summary["outside_us_or_unknown"])
        self.assertEqual("outside_us", FakeState.instance.ignored[0][1])
        post.assert_not_called()

    @patch.object(run, "DynamoSeenJobs", FakeState)
    @patch.object(run, "github_list_revision", return_value="")
    @patch.object(run, "fetch_all", return_value=([{
        **JOBS[0],
        "location": "Miami, FL",
        "posted": "2020-01-01",
        "url": "https://jobs.example.test/stale-intern-1",
    }], []))
    def test_stale_job_is_remembered_without_delivery(self, _fetch, _revision):
        urls = {category.key: f"https://discord.com/api/webhooks/{category.key}/secret" for category in run.CATEGORIES}
        with self.env(), patch.object(run, "_webhook_urls", return_value=urls), \
                patch.object(run, "post_to_discord") as post:
            summary = run.execute()
        self.assertEqual(1, summary["stale"])
        self.assertEqual("stale", FakeState.instance.ignored[0][1])
        post.assert_not_called()

    def test_webhook_test_posts_once_per_category(self):
        urls = {category.key: f"https://discord.com/api/webhooks/{category.key}/secret" for category in run.CATEGORIES}
        with patch.object(run, "_webhook_urls", return_value=urls), \
                patch.object(run, "post_to_discord") as post:
            summary = run.lambda_handler({"test_webhooks": True}, None)
        self.assertEqual(5, summary["count"])
        self.assertEqual(5, post.call_count)
        self.assertEqual("Dec 2027 – Jun 2028", summary["sample_graduation"])
        for call in post.call_args_list:
            fields = {
                field["name"]: field["value"]
                for field in call.args[1]["embeds"][0]["fields"]
            }
            self.assertEqual("Dec 2027 – Jun 2028", fields["Graduation"])

    def test_five_minute_slot(self):
        top_of_hour = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
        first = run._five_minute_slot(top_of_hour)
        self.assertEqual(first + 1, run._five_minute_slot(top_of_hour + dt.timedelta(minutes=5)))
        self.assertEqual(first + 12, run._five_minute_slot(top_of_hour + dt.timedelta(hours=1)))

    @patch.object(run, "execute", return_value={"seeded": 0})
    def test_partitioned_seed_event(self, execute):
        result = run.lambda_handler({
            "seed_only": True,
            "seed_partition": 3,
            "seed_partition_count": 12,
        }, None)
        self.assertEqual({"seeded": 0}, result)
        execute.assert_called_once_with(
            seed_only=True,
            only_sources=None,
            seed_partition=(3, 12),
        )

    def test_partition_requires_seed_mode(self):
        with self.assertRaisesRegex(ValueError, "requires seed_only"):
            run.lambda_handler({"seed_partition": 0, "seed_partition_count": 12}, None)

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

    @patch.object(run, "github_list_revision", return_value="same-sha")
    def test_seed_forces_unchanged_github_list_fetch(self, _revision):
        class RevisionState:
            def source_revisions(self, _names):
                raise AssertionError("forced seed should not need prior revisions")

        config = {"list": [{"name": "Large list", "urls": ["https://example.test/jobs.json"]}]}
        filtered, pending, cached = run._skip_unchanged_lists(config, RevisionState(), force=True)
        self.assertEqual(1, len(filtered["list"]))
        self.assertEqual({"Large list": "same-sha"}, pending)
        self.assertEqual([], cached)


if __name__ == "__main__":
    unittest.main()

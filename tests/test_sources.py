import unittest
from unittest.mock import patch

from jobfinder import sources
from jobfinder.util import parse_ats_url


class SourceAdapterTests(unittest.TestCase):
    @patch.object(sources, "get_json", return_value={"sha": "abc123"})
    def test_github_list_revision(self, get_json):
        revision = sources.github_list_revision({
            "urls": ["https://raw.githubusercontent.com/example/jobs/main/jobs.json"]
        })
        self.assertEqual("abc123", revision)
        self.assertIn("/repos/example/jobs/commits/main", get_json.call_args.args[0])

    @patch.object(sources, "get_json")
    def test_greenhouse_listing_defers_description(self, get_json):
        get_json.return_value = {
            "jobs": [{
                "id": 123,
                "title": "Software Intern",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
                "location": {"name": "Remote"},
                "content": "large description that should not be copied",
            }]
        }
        jobs = sources.fetch_greenhouse("acme", "Acme")
        self.assertEqual("", jobs[0]["description"])
        self.assertNotIn("content=true", get_json.call_args.args[0])

    @patch.object(sources, "get_json")
    def test_smartrecruiters_listing(self, get_json):
        get_json.return_value = {
            "totalFound": 1,
            "content": [{
                "id": "743999",
                "name": "Data Engineering Intern",
                "releasedDate": "2026-10-01T00:00:00Z",
                "company": {"identifier": "Acme"},
                "location": {"fullLocation": "Boston, MA"},
                "typeOfEmployment": {"label": "Intern"},
            }],
        }
        jobs = sources.fetch_smartrecruiters("Acme", "Acme Corp")
        self.assertEqual(1, len(jobs))
        self.assertEqual("Intern", jobs[0]["employment_type"])
        self.assertEqual("smartrecruiters", parse_ats_url(jobs[0]["url"])["ats"])

    @patch.object(sources, "get_json")
    def test_rippling_merges_locations(self, get_json):
        get_json.return_value = [
            {"uuid": "12345678-1234-1234-1234-123456789abc", "name": "SWE Intern",
             "workLocation": {"label": "New York"}, "url": "https://ats.rippling.com/acme/jobs/12345678-1234-1234-1234-123456789abc"},
            {"uuid": "12345678-1234-1234-1234-123456789abc", "name": "SWE Intern",
             "workLocation": {"label": "Remote"}, "url": "https://ats.rippling.com/acme/jobs/12345678-1234-1234-1234-123456789abc"},
        ]
        jobs = sources.fetch_rippling("acme", "Acme")
        self.assertEqual(1, len(jobs))
        self.assertEqual("New York; Remote", jobs[0]["location"])
        self.assertEqual("rippling", parse_ats_url(jobs[0]["url"])["ats"])

    @patch.object(sources.time, "sleep")
    @patch.object(sources, "fetch_greenhouse", return_value=[])
    def test_fetch_all_respects_tier(self, fetch_greenhouse, _sleep):
        cfg = {"greenhouse": [
            {"name": "Fast", "board": "fast", "tier": 1},
            {"name": "Slow", "board": "slow", "tier": 3},
        ]}
        _, statuses = sources.fetch_all(cfg, tiers={1})
        self.assertEqual(["Fast"], [status["name"] for status in statuses])
        fetch_greenhouse.assert_called_once_with("fast", "Fast")

    @patch.object(sources.time, "sleep")
    @patch.object(sources, "fetch_greenhouse", return_value=[])
    def test_fetch_all_staggers_lower_tiers(self, fetch_greenhouse, _sleep):
        cfg = {"greenhouse": [
            {"name": "Always", "board": "always", "tier": 1},
            {"name": "Quarter Hour", "board": "quarter", "tier": 2},
            {"name": "Hourly", "board": "hourly", "tier": 3},
        ]}
        observed = {name: 0 for name in ("Always", "Quarter Hour", "Hourly")}
        for slot in range(12):
            _, statuses = sources.fetch_all(cfg, tiers={1, 2, 3}, schedule_slot=slot)
            for status in statuses:
                observed[status["name"]] += 1
        self.assertEqual({"Always": 12, "Quarter Hour": 4, "Hourly": 1}, observed)

    @patch.object(sources.time, "sleep")
    @patch.object(sources, "fetch_greenhouse", return_value=[])
    def test_named_source_bypasses_staggering(self, fetch_greenhouse, _sleep):
        cfg = {"greenhouse": [{"name": "Hourly", "board": "hourly", "tier": 3}]}
        _, statuses = sources.fetch_all(
            cfg, tiers={1, 2, 3}, only={"Hourly"}, schedule_slot=0
        )
        self.assertEqual(["Hourly"], [status["name"] for status in statuses])


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest.mock import patch

import run


def status(ok=True, count=10, error=""):
    return [{
        "name": "Example Co",
        "kind": "Greenhouse",
        "tier": 1,
        "group": "software",
        "ok": ok,
        "count": count,
        "error": error,
    }]


class HealthState:
    def __init__(self):
        self.records = {}
        self.heartbeat_days = set()

    def source_health(self, names):
        return {name: self.records[name] for name in names if name in self.records}

    def save_source_health(self, name, values):
        self.records[name] = dict(values)

    def claim_daily_heartbeat(self, day):
        if day in self.heartbeat_days:
            return False
        self.heartbeat_days.add(day)
        return True


SUMMARY = {
    "sources_checked": 1,
    "raw_jobs": 10,
    "delivered": 0,
    "failed_sources": [],
}


class SourceHealthTests(unittest.TestCase):
    def monitor(self, state, statuses):
        with patch.object(run, "post_to_discord") as post, patch.dict(
            os.environ, {"HEALTH_FAILURE_THRESHOLD": "3"}, clear=False
        ):
            result = run._monitor_source_health(
                state, statuses, "https://discord.com/api/webhooks/health/secret", SUMMARY
            )
        return result, post

    def test_alerts_after_three_failures_then_recovers(self):
        state = HealthState()
        first, first_post = self.monitor(state, status(False, 0, "HTTP 500"))
        second, second_post = self.monitor(state, status(False, 0, "HTTP 500"))
        third, third_post = self.monitor(state, status(False, 0, "HTTP 500"))

        self.assertEqual(0, first["health_events"])
        self.assertEqual(0, second["health_events"])
        self.assertEqual(1, third["health_events"])
        # The first invocation also sends the once-per-day heartbeat.
        self.assertEqual(1, first_post.call_count)
        self.assertEqual(0, second_post.call_count)
        self.assertEqual(1, third_post.call_count)
        self.assertTrue(state.records["Example Co"]["failure_alerted"])

        recovered, recovered_post = self.monitor(state, status(True, 12))
        self.assertEqual(1, recovered["health_events"])
        self.assertEqual(1, recovered["health_recoveries"])
        self.assertEqual(1, recovered_post.call_count)
        self.assertFalse(state.records["Example Co"]["failure_alerted"])

    def test_zero_alert_requires_prior_nonzero_result(self):
        state = HealthState()
        self.monitor(state, status(True, 0))
        self.assertNotIn("Example Co", state.records)

        self.monitor(state, status(True, 8))
        self.monitor(state, status(True, 0))
        self.monitor(state, status(True, 0))
        result, post = self.monitor(state, status(True, 0))
        self.assertEqual(1, result["health_events"])
        self.assertEqual(1, post.call_count)
        self.assertTrue(state.records["Example Co"]["zero_alerted"])

    def test_health_webhook_test_mode(self):
        with patch.object(
            run,
            "_health_webhook_url",
            return_value="https://discord.com/api/webhooks/health/secret",
        ), patch.object(run, "post_to_discord") as post:
            result = run.lambda_handler({"test_health_webhook": True}, None)
        self.assertEqual({"health_webhook_test": "sent"}, result)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()

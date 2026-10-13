import json
import unittest
from unittest.mock import patch

from jobfinder import custom_sources


class CustomSourceTests(unittest.TestCase):
    @patch.object(custom_sources, "request")
    def test_apple_hydration(self, request):
        payload = {
            "loaderData": {"search": {
                "totalRecords": 1,
                "searchResults": [{
                    "positionId": "200123",
                    "reqId": "PIPE-200123",
                    "postingTitle": "Software Engineering Intern",
                    "transformedPostingTitle": "software-engineering-intern",
                    "postDateInGMT": "2026-10-12T00:00:00Z",
                    "locations": [{"name": "Cupertino, CA", "countryName": "United States"}],
                    "homeOffice": False,
                }],
            }},
        }
        encoded = json.dumps(json.dumps(payload))
        request.return_value = f"<script>window.__staticRouterHydrationData = JSON.parse({encoded});</script>"
        jobs = custom_sources.fetch_apple("Apple", max_pages=1)
        self.assertEqual(1, len(jobs))
        self.assertEqual("United States", jobs[0]["country"])
        self.assertEqual("PIPE-200123", jobs[0]["source_job_id"])

    def test_google_ds1_payload(self):
        body = "prefix AF_initDataCallback({key: 'ds:1', data:[[[]],null,1,20], sideChannel: {}});"
        payload = custom_sources._google_payload(body)
        self.assertEqual(1, payload[2])

    @patch.object(custom_sources, "request")
    def test_citadel_cards(self, request):
        request.return_value = json.dumps({
            "number_of_post": 1,
            "found_posts": 1,
            "content": (
                '<a href="https://www.citadel.com/careers/details/software-intern/" '
                'data-position="Software Engineer Intern"><span class="careers-listing-card__location">'
                'New York, NY</span></a>'
            ),
        })
        jobs = custom_sources.fetch_citadel("citadel", "Citadel")
        self.assertEqual(1, len(jobs))
        self.assertEqual("software-intern", jobs[0]["source_job_id"])

    def test_turbo_value_resolves_reference_objects(self):
        values = ["title", "Software Intern", {"_0": 1}]
        self.assertEqual({"title": "Software Intern"}, custom_sources._turbo_value(values, 2))


if __name__ == "__main__":
    unittest.main()

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

    @patch.object(custom_sources, "request")
    def test_pwc_phenom_payload(self, request):
        payload = {
            "eagerLoadRefineSearch": {"data": {
                "totalHits": 1,
                "jobs": [{
                    "title": "Tax Intern",
                    "multi_location": ["Tampa, Florida"],
                    "applyUrl": "https://pwc.wd3.myworkdayjobs.com/job/123",
                    "postedDate": "2026-10-12T00:00:00Z",
                    "country": "United States",
                    "descriptionTeaser": "Assist with tax compliance.",
                    "jobId": "123",
                }],
            }},
        }
        request.return_value = (
            "<script>phApp.ddo = " + json.dumps(payload)
            + "; phApp.experimentData = {};</script>"
        )
        jobs = custom_sources.fetch_pwc("PwC", max_pages=1)
        self.assertEqual(1, len(jobs))
        self.assertEqual("Tax Intern", jobs[0]["title"])
        self.assertEqual("2026-10-12", jobs[0]["posted"])

    @patch.object(custom_sources, "request")
    def test_structured_job_detail(self, request):
        posting = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Audit Intern",
            "datePosted": "Oct 1, 2026",
            "description": "<p>Accounting and GAAP.</p>",
            "identifier": {"value": "A-123"},
            "jobLocation": [{"address": {
                "addressLocality": "Miami",
                "addressRegion": "FL",
                "addressCountry": "US",
            }}],
        }
        request.return_value = (
            '<script type="application/ld+json">' + json.dumps(posting) + "</script>"
        )
        jobs = custom_sources._details_to_jobs(
            "Example Accounting", ["https://example.test/job/123"], workers=1
        )
        self.assertEqual("Miami, FL", jobs[0]["location"])
        self.assertEqual("US", jobs[0]["country"])
        self.assertEqual("2026-10-01", jobs[0]["posted"])
        self.assertEqual("A-123", jobs[0]["source_job_id"])

    def test_turbo_value_resolves_reference_objects(self):
        values = ["title", "Software Intern", {"_0": 1}]
        self.assertEqual({"title": "Software Intern"}, custom_sources._turbo_value(values, 2))


if __name__ == "__main__":
    unittest.main()

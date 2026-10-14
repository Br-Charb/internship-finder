import datetime as dt
import unittest

from jobfinder.match import (
    categorize_job,
    is_phd_exclusive_job,
    is_recent_posting,
    is_target_job,
    is_united_states_job,
    requirements_snippet,
)


class CategorizationTests(unittest.TestCase):
    def assert_category(self, expected: str, title: str, description: str = "") -> None:
        category, _ = categorize_job(title, description)
        self.assertIsNotNone(category)
        self.assertEqual(expected, category.key)

    def test_software(self) -> None:
        self.assert_category("software", "Frontend Software Engineering Intern", "React and TypeScript")
        self.assert_category("software", "SWE Intern")

    def test_hardware(self) -> None:
        self.assert_category("hardware", "Embedded Firmware Intern", "FPGA, PCB, and C++")
        self.assert_category("hardware", "ASIC Design Verification Intern", "SystemVerilog and RTL")
        self.assert_category("hardware", "Electrical Engineering Intern", "Altium schematic capture")
        self.assert_category("hardware", "Mechatronics Intern", "Sensors, actuators, and motor control")

    def test_mechanical(self) -> None:
        self.assert_category("mechanical", "Mechanical Design Intern", "CAD and SolidWorks")
        self.assert_category("mechanical", "Thermal Analysis Intern", "ANSYS and heat transfer")
        self.assert_category("mechanical", "Manufacturing Engineering Intern", "GD&T and CNC machining")
        self.assert_category("mechanical", "Structures Intern", "FEA, NASTRAN, and composites")

    def test_aerospace_beats_generic_analyst(self) -> None:
        self.assert_category("aerospace", "Propulsion Analyst Intern", "Rocket engine analysis")

    def test_finance(self) -> None:
        self.assert_category("finance", "FP&A Summer Analyst", "Financial modeling and accounting")
        self.assert_category("finance", "IT Intern")
        self.assert_category("finance", "Quantitative Trading Intern", "Derivatives and financial markets")
        self.assert_category("finance", "Payments Risk Analyst Intern", "Credit risk and fraud detection")
        self.assert_category("finance", "Business Systems Analyst Intern", "SAP and Power BI")

    def test_accounting(self) -> None:
        self.assert_category("accounting", "Audit Intern", "Public accounting and GAAP")
        self.assert_category("accounting", "Tax Internship", "Tax compliance and CPA track")
        self.assert_category(
            "accounting",
            "Assurance Summer Intern",
            "Review financial statements and internal controls.",
        )

    def test_accounting_beats_generic_finance(self) -> None:
        self.assert_category(
            "accounting",
            "Accounting and Financial Reporting Intern",
            "Prepare journal entries and reconciliations.",
        )

    def test_description_requires_two_signals(self) -> None:
        category, _ = categorize_job("General Engineering Intern", "Some Python exposure is useful")
        self.assertIsNone(category)
        self.assert_category("software", "General Engineering Intern", "Python and React experience")

    def test_primary_match_only(self) -> None:
        category, _ = categorize_job(
            "Flight Software Engineer Intern",
            "Build Python flight control services for spacecraft.",
        )
        self.assertEqual("software", category.key)


class ParsingTests(unittest.TestCase):
    def test_internship_scope(self) -> None:
        self.assertTrue(is_target_job("Software Engineer Intern"))
        self.assertTrue(is_target_job("Summer Finance Analyst"))
        self.assertFalse(is_target_job("Senior Software Engineer"))
        self.assertTrue(is_target_job("Senior Software Engineer", scope="all"))
        self.assertTrue(is_target_job("Engineering Rotation", employment_type="Internship"))
        self.assertTrue(is_target_job("New Graduate Software Engineer"))
        self.assertFalse(is_target_job("Software Sales Intern"))

    def test_strict_us_locations(self) -> None:
        accepted = (
            "Seattle, WA",
            "New York, New York, United States of America",
            "Remote - US",
            "Toronto, Canada; Austin, TX",
            "California",
            "Washington, DC",
        )
        rejected = ("Remote", "London, UK", "Toronto, Canada", "Multiple locations", "")
        for location in accepted:
            with self.subTest(location=location):
                self.assertTrue(is_united_states_job(location))
        for location in rejected:
            with self.subTest(location=location):
                self.assertFalse(is_united_states_job(location))

    def test_posting_freshness(self) -> None:
        today = dt.date(2026, 10, 13)
        self.assertTrue(is_recent_posting("2026-10-01", 14, today))
        self.assertFalse(is_recent_posting("2026-09-01", 14, today))
        self.assertTrue(is_recent_posting("", 14, today))
        self.assertTrue(is_recent_posting("not supplied", 14, today))
        self.assertTrue(is_recent_posting("2020-01-01", 0, today))

    def test_requirement_section(self) -> None:
        description = """About us
We build spacecraft.
Qualifications:
- Python experience
- Enrolled in a BS degree
- Excellent communication
Responsibilities
- Build things
"""
        snippet = requirements_snippet(description)
        self.assertIn("Python experience", snippet)
        self.assertIn("BS degree", snippet)
        self.assertNotIn("Build things", snippet)

    def test_phd_exclusive_filter_is_conservative(self) -> None:
        self.assertTrue(is_phd_exclusive_job("Machine Learning PhD Intern"))
        self.assertFalse(is_phd_exclusive_job("BS/MS/PhD Research Intern"))
        self.assertTrue(is_phd_exclusive_job(
            "Research Intern",
            "Applicants must be currently pursuing a doctoral degree in computer science.",
        ))
        self.assertFalse(is_phd_exclusive_job(
            "Research Intern",
            "Applicants may be pursuing a bachelor's, master's, or PhD degree.",
        ))
        self.assertFalse(is_phd_exclusive_job(
            "Engineering Intern",
            "Work with PhD researchers while pursuing a BS or MS degree.",
        ))


if __name__ == "__main__":
    unittest.main()

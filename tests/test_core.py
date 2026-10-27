"""Tests for the plain-code decisions. No model and no ADK needed: python -m unittest"""

import tempfile
import unittest
from pathlib import Path

from php2shell_response import core

ADVISORY = core.load_advisory()


class StatusTests(unittest.TestCase):
    def test_below_fix_is_affected(self):
        self.assertEqual(core.status_for("8.4.20", ADVISORY)["status"], "affected")
        self.assertEqual(core.status_for("8.4.20", ADVISORY)["target"], "8.4.27")

    def test_fixed_version_is_patched(self):
        for v in ("8.5.12", "8.4.27", "8.3.36", "8.2.35"):
            self.assertEqual(core.status_for(v, ADVISORY)["status"], "patched", v)

    def test_newer_than_fix_is_patched(self):
        self.assertEqual(core.status_for("8.3.40", ADVISORY)["status"], "patched")

    def test_branch_without_release_is_end_of_life(self):
        for v in ("8.1.31", "7.4.33", "5.6.40"):
            self.assertEqual(core.status_for(v, ADVISORY)["status"], "end_of_life", v)

    def test_versions_compare_as_numbers_not_text(self):
        # "8.4.9" < "8.4.27" numerically, though not as text
        self.assertEqual(core.status_for("8.4.9", ADVISORY)["status"], "affected")


class FleetTests(unittest.TestCase):
    def test_sample_fleet(self):
        matched = core.match_fleet(core.load_fleet(), ADVISORY)
        need = sorted(s["site"] for s in matched if s["needs_action"])
        self.assertEqual(
            need,
            sorted([
                "shop-frontend", "blog", "partner-api", "legacy-forum",
                "intranet-wiki", "staging-shop", "build-runner", "old-campaign-site",
            ]),
        )

    def test_priority(self):
        self.assertEqual(core.priority({"internet_facing": "yes", "data_class": "customer-pii"}), "P1")
        self.assertEqual(core.priority({"internet_facing": "yes", "data_class": "public"}), "P2")
        self.assertEqual(core.priority({"internet_facing": "no", "data_class": "internal"}), "P3")


class PlanCheckTests(unittest.TestCase):
    def test_missing_sites_are_reported(self):
        plan = "| P1 | shop-frontend | 8.4.20 | 8.4.27 |"
        self.assertEqual(core.missing_from_plan(plan, ["shop-frontend", "blog"]), ["blog"])

    def test_complete_plan(self):
        self.assertEqual(core.missing_from_plan("blog and shop-frontend", ["shop-frontend", "blog"]), [])


class WriteBoundsTests(unittest.TestCase):
    def test_name_is_reduced_to_base_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = core.safe_report_path("../../outside", Path(d))
            self.assertEqual(p.parent, Path(d).resolve())
            self.assertEqual(p.name, "outside.md")

    def test_invalid_names_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("", ".", ".."):
                with self.assertRaises(ValueError):
                    core.safe_report_path(bad, Path(d))

    def test_unapproved_plan_is_marked(self):
        with tempfile.TemporaryDirectory() as d:
            p = core.write_report("# plan", Path(d), approved=False)
            self.assertTrue(p.name.endswith("-NEEDS-REVIEW.md"))

    def test_write_lands_in_reports_folder(self):
        with tempfile.TemporaryDirectory() as d:
            p = core.write_report("# plan", Path(d))
            self.assertTrue(p.exists())
            self.assertEqual(p.parent, Path(d).resolve())


if __name__ == "__main__":
    unittest.main()

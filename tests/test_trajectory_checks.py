"""The end-to-end trajectory checks, tested offline on hand-made traces (no model)."""

import unittest

from e2e import run_campaign as r
from php2shell_response import core

AFFECTED = [{"site": s["site"]} for s in core.match_fleet(core.load_fleet(), core.load_advisory()) if s["needs_action"]]


def ev(**k):
    base = {"author": "x", "calls": [], "responses": [], "text": "", "state_delta": {}, "transfer_to": None,
            "escalate": False, "prompt_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    return {**base, **k}


GOOD = [
    ev(author="intake", transfer_to="response_team"),
    ev(author="advisory_reader", responses=[{"name": "read_advisory", "response": {}}]),
    ev(author="exposure_matcher", responses=[{"name": "match_versions", "response": {}}]),
    ev(author="exposure_analyst"),
    ev(author="patch_analyst"),
    ev(author="planner", state_delta={"plan": "p"}),
    ev(author="reviewer", responses=[{"name": "check_plan", "response": {"complete": True}}]),
    ev(author="reviewer", responses=[{"name": "approve_plan", "response": {"status": "approved"}}]),
    ev(author="report_writer", responses=[{"name": "save_report", "response": {"status": "saved", "path": "reports/x.md"}}]),
]
STATE = {"exposure_report": "a", "patch_report": "b", "affected_sites": AFFECTED, "approved": True}


def failures(trace, state=STATE):
    return [name for name, ok, _ in r.check(trace, state) if not ok]


class TrajectoryCheckTests(unittest.TestCase):
    def test_good_trace_passes(self):
        self.assertEqual(failures(GOOD), [])

    def test_approval_before_the_check_fails(self):
        bad = list(GOOD)
        bad.insert(6, bad.pop(5))  # the planner's new draft lands after the check, so the approval has no check behind it
        self.assertIn("loop: approval only after check_plan passed on that draft", failures(bad))

    def test_wrong_route_fails(self):
        bad = [ev(author="intake", transfer_to="asset_lookup")] + GOOD[1:]
        self.assertIn("routing: a full response goes to response_team", failures(bad))

    def test_missing_branch_fails(self):
        self.assertIn("fan out: both analysts ran and saved their own key", failures(GOOD, {**STATE, "patch_report": ""}))

    def test_second_save_fails(self):
        self.assertIn("writes: save_report ran once, inside reports/", failures(GOOD + [GOOD[-1]]))


if __name__ == "__main__":
    unittest.main()

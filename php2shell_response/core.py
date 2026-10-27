"""Plain-code logic for the php2shell response team.

Nothing here calls a model. The agents call these functions through tools, so the
decisions that must be exact (is a site affected? where may a file be written? does the
plan cover every site?) are made by code and can be unit-tested.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


def load_advisory(path: Path = DATA_DIR / "advisory.json") -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fleet(path: Path = DATA_DIR / "fleet.csv") -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _parse(version: str) -> tuple[int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", version)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def status_for(php_version: str, advisory: dict) -> dict:
    """Decide, in code, whether one PHP version is affected and what fixes it."""
    major, minor, patch = _parse(php_version)
    branch = f"{major}.{minor}"
    fixed = advisory["fixed_versions"].get(branch)
    if fixed is None:
        return {
            "status": "end_of_life",
            "branch": branch,
            "target": "upgrade to a supported branch (8.2 or later)",
            "note": "no official release on this branch; " + advisory["end_of_life_note"],
        }
    if (major, minor, patch) < _parse(fixed):
        return {"status": "affected", "branch": branch, "target": fixed, "note": ""}
    return {"status": "patched", "branch": branch, "target": fixed, "note": ""}


def match_fleet(fleet: list[dict], advisory: dict) -> list[dict]:
    """Every site with its status. Affected and end-of-life sites both need action."""
    out = []
    for site in fleet:
        result = status_for(site["php_version"], advisory)
        out.append({**site, **result, "needs_action": result["status"] != "patched"})
    return out


def priority(site: dict) -> str:
    """A simple, explainable ranking: exposure first, then the data at risk."""
    exposed = site.get("internet_facing") == "yes"
    sensitive = site.get("data_class") in ("customer-pii", "payment")
    if exposed and sensitive:
        return "P1"
    if exposed or sensitive:
        return "P2"
    return "P3"


def missing_from_plan(plan: str, sites_needing_action: list[str]) -> list[str]:
    """Sites the plan doesn't mention at all. The reviewer exits the loop only when this is empty."""
    text = plan.lower()
    return [s for s in sites_needing_action if s.lower() not in text]


def safe_report_path(name: str, reports_dir: Path = REPORTS_DIR) -> Path:
    """The only place the system writes. The folder is fixed in code; the name is reduced
    to a plain base name; anything that resolves outside the folder is refused."""
    base = Path(name).name
    if not base or base in (".", ".."):
        raise ValueError("empty or invalid file name")
    if not base.endswith(".md"):
        base += ".md"
    target = (reports_dir / base).resolve()
    if reports_dir.resolve() not in target.parents:
        raise ValueError("path resolves outside the reports folder")
    return target


def write_report(content: str, reports_dir: Path = REPORTS_DIR, approved: bool = True) -> Path:
    """Save the plan. A plan the reviewer didn't approve is saved under a NEEDS-REVIEW name."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "" if approved else "-NEEDS-REVIEW"
    target = safe_report_path(f"php2shell-plan-{stamp}{suffix}.md", reports_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target

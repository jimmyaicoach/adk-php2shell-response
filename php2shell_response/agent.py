"""php2shell response team: an ADK multi-agent workflow for a critical PHP advisory.

Defensive only. The agents read a public advisory and a sample fleet, decide exposure,
and write a prioritised patch plan for a person to approve. No tool changes a server.

Five ADK patterns in one system:
  1. a root agent that routes to sub-agents          (intake)
  2. shared session state                            (advisory, affected_sites, plan, ...)
  3. steps in a fixed order                          (response_team: SequentialAgent)
  4. a loop with a checker and a cap                 (plan_room: LoopAgent)
Each response-team agent keeps its fixed rules in static_instruction (sent first, the part a cache
can reuse) and only the changing state in instruction (sent after it), ending with the task line,
so the question comes after the data, as Google's Gemini 3 guidance recommends.
  5. fan out and gather                              (analysis: ParallelAgent)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from . import core

load_dotenv()

MODEL_NAME = os.getenv("MODEL", "gemini-3.8-flash")
RETRY = types.HttpRetryOptions(initial_delay=2, max_delay=20, attempts=5)


def model() -> Gemini:
    return Gemini(model=MODEL_NAME, retry_options=RETRY)


# --------------------------------------------------------------------------- tools
# Each tool is a thin wrapper over core.py. State is written by assignment, so every
# change is recorded as an event with its state delta.

def read_advisory(tool_context: ToolContext) -> dict:
    """Load the PHP2Shell advisory (public facts and sources) into session state."""
    advisory = core.load_advisory()
    tool_context.state["advisory"] = advisory
    return {
        "cves": advisory["cves"],
        "severity": advisory["severity"],
        "fixed_versions": advisory["fixed_versions"],
        "exploitation": advisory["exploitation"],
        "sources": advisory["sources"],
    }


def match_versions(tool_context: ToolContext) -> dict:
    """Compare every site's PHP version with the advisory, in code, and save the sites that need action."""
    advisory = tool_context.state.get("advisory") or core.load_advisory()
    matched = core.match_fleet(core.load_fleet(), advisory)
    needs_action = [
        {
            "site": s["site"],
            "php_version": s["php_version"],
            "status": s["status"],
            "target": s["target"],
            "priority": core.priority(s),
            "owner": s["owner"],
            "environment": s["environment"],
            "internet_facing": s["internet_facing"],
            "data_class": s["data_class"],
        }
        for s in matched
        if s["needs_action"]
    ]
    tool_context.state["affected_sites"] = needs_action
    return {
        "sites_checked": len(matched),
        "need_action": len(needs_action),
        "already_patched": [s["site"] for s in matched if not s["needs_action"]],
    }


def lookup_site(site: str, tool_context: ToolContext) -> dict:
    """Look up one site by name and say whether it is affected, and what fixes it."""
    advisory = tool_context.state.get("advisory") or core.load_advisory()
    for s in core.load_fleet():
        if s["site"] == site:
            return {**s, **core.status_for(s["php_version"], advisory), "priority": core.priority(s)}
    return {"error": f"no site named {site!r} in the fleet"}


def check_plan(tool_context: ToolContext) -> dict:
    """Check, in code, that the current plan names every site that needs action."""
    plan = tool_context.state.get("plan", "")
    names = [s["site"] for s in tool_context.state.get("affected_sites", [])]
    missing = core.missing_from_plan(plan, names)
    tool_context.state["plan_complete"] = not missing
    return {"complete": not missing, "missing_sites": missing}


def approve_plan(tool_context: ToolContext) -> dict:
    """Approve the plan and end the review loop. Refused unless check_plan passed on this plan."""
    if not tool_context.state.get("plan_complete"):
        return {"status": "refused", "reason": "check_plan has not passed on the current plan"}
    tool_context.state["approved"] = True
    tool_context.actions.escalate = True  # ends the LoopAgent, as ADK's exit_loop does
    tool_context.actions.skip_summarization = True
    return {"status": "approved"}


def save_report(tool_context: ToolContext) -> dict:
    """Save the approved plan to the reports folder. The folder and file name are set in code."""
    plan = tool_context.state.get("plan", "")
    if not plan:
        return {"status": "refused", "reason": "there is no plan in state to save"}
    approved = bool(tool_context.state.get("approved"))
    path = core.write_report(plan, approved=approved)
    status = "saved" if approved else "saved, needs review: the loop ended before the plan passed check_plan"
    return {"status": status, "path": str(path.relative_to(core.ROOT))}


# ----------------------------------------------------------- step checks (callbacks)

def require(*keys: str):
    """Before a step runs, check the earlier steps left their output. If not, stop here
    with a clear message instead of letting the next agent work from nothing."""

    def check(callback_context: CallbackContext) -> types.Content | None:
        state = callback_context.state
        if state.get("halted"):
            return types.Content(role="model", parts=[types.Part(text="Skipped: an earlier step stopped the run.")])
        missing = [k for k in keys if not state.get(k)]
        if missing:
            state["halted"] = True
            return types.Content(
                role="model",
                parts=[types.Part(text=f"Stopped before {callback_context.agent_name}: missing {', '.join(missing)} in state.")],
            )
        return None

    return check


def reset_review(callback_context: CallbackContext) -> None:
    """A new draft must pass check_plan again before it can be approved."""
    callback_context.state["plan_complete"] = False
    return None


# ------------------------------------------------------------- the response team

advisory_reader = LlmAgent(
    name="advisory_reader",
    model=model(),
    include_contents="none",
    description="Reads the php2shell advisory into session state.",
    static_instruction=(
        "Call read_advisory once. Then summarise the advisory in three short lines: the flaws, "
        "the severity, and the fixed versions. Cite one source URL."
    ),
    tools=[read_advisory],
)

exposure_matcher = LlmAgent(
    name="exposure_matcher",
    model=model(),
    include_contents="none",
    description="Matches every site in the fleet against the advisory, in code.",
    static_instruction="Call match_versions once, then report how many sites need action. Don't judge versions yourself.",
    tools=[match_versions],
    before_agent_callback=require("advisory"),
)

exposure_analyst = LlmAgent(
    name="exposure_analyst",
    model=model(),
    include_contents="none",
    description="Ranks the affected sites by exposure and data at risk.",
    static_instruction=(
        "You rank the sites that need action in a PHP2Shell response. Group them by priority "
        "(P1 first). For each, give one line: site, why it has that priority (internet-facing, data "
        "class), and the owner. Use only the site data you are given."
    ),
    instruction="Sites that need action:\n{affected_sites}\n\nUsing the data above, do the task in your instructions.",
    output_key="exposure_report",
    before_agent_callback=require("affected_sites"),
)

patch_analyst = LlmAgent(
    name="patch_analyst",
    model=model(),
    include_contents="none",
    description="Works out the target version or upgrade path for each affected site.",
    static_instruction=(
        "You work out the patch target for each site that needs action in a PHP2Shell response. "
        "For each site give the target from the data: a fixed version on its branch, or for an "
        "end-of-life branch an upgrade to a supported branch. Note staging and CI sites separately."
    ),
    instruction="Advisory:\n{advisory}\n\nSites that need action:\n{affected_sites}\n\nUsing the data above, do the task in your instructions.",
    output_key="patch_report",
    before_agent_callback=require("advisory", "affected_sites"),
)

analysis = ParallelAgent(
    name="analysis",
    description="Exposure and patch analysis, run at the same time, each saved under its own key.",
    sub_agents=[exposure_analyst, patch_analyst],
)

planner = LlmAgent(
    name="planner",
    model=model(),
    include_contents="none",
    description="Drafts the patch plan.",
    static_instruction=(
        "You write the patch plan for a PHP2Shell response. Write it as a Markdown table with one "
        "row per site: priority, site, current version, target, owner, rollout (staging first, then "
        "a small canary, then the rest), and rollback (keep the previous runtime ready). Order by "
        "priority. Then add one line: 'A person approves each change before it runs.' If there is "
        "reviewer feedback, fix every point in it. Output only the plan."
    ),
    instruction=(
        "Exposure:\n{exposure_report}\n\nPatches:\n{patch_report}\n\n"
        "Reviewer feedback from the last round (may be empty):\n{feedback?}"
        "\n\nUsing the data above, do the task in your instructions."
    ),
    output_key="plan",
    before_agent_callback=require("exposure_report", "patch_report"),
    after_agent_callback=reset_review,
)

reviewer = LlmAgent(
    name="reviewer",
    model=model(),
    include_contents="none",
    description="Checks the plan and ends the loop when it is complete.",
    static_instruction=(
        "You review a PHP2Shell patch plan. First call check_plan. If it reports missing sites, list "
        "them as feedback. Also check that every row has a target, an owner, a rollout and a "
        "rollback, and that P1 sites come first. If check_plan is complete and every row passes, "
        "call approve_plan. Otherwise reply with short, specific feedback only."
    ),
    instruction="Plan:\n{plan}\n\nUsing the data above, do the task in your instructions.",
    tools=[check_plan, approve_plan],
    output_key="feedback",
    before_agent_callback=require("plan"),
)

plan_room = LoopAgent(
    name="plan_room",
    description="Draft and review the plan until the reviewer approves it, at most three rounds.",
    sub_agents=[planner, reviewer],
    max_iterations=3,
)

report_writer = LlmAgent(
    name="report_writer",
    model=model(),
    include_contents="none",
    description="Saves the reviewed plan to the reports folder.",
    static_instruction=(
        "Call save_report once, then tell the user where the plan was saved, whether it passed review, "
        "and that a person must approve each change."
    ),
    tools=[save_report],
    before_agent_callback=require("plan"),
)

response_team = SequentialAgent(
    name="response_team",
    description="Runs a full php2shell response: read the advisory, match the fleet, analyse, plan and save the report.",
    sub_agents=[advisory_reader, exposure_matcher, analysis, plan_room, report_writer],
)

# ------------------------------------------------------------ quick-answer agents

advisory_analyst = LlmAgent(
    name="advisory_analyst",
    model=model(),
    description="Answers questions about the php2shell advisory itself.",
    instruction="Call read_advisory, then answer the question from it. Cite a source URL.",
    tools=[read_advisory],
)

asset_lookup = LlmAgent(
    name="asset_lookup",
    model=model(),
    description="Says whether one named site is affected and what fixes it.",
    instruction="Call lookup_site with the site name the user gave, then answer in two lines.",
    tools=[lookup_site],
)

# ------------------------------------------------------------------- the root

root_agent = LlmAgent(
    name="intake",
    model=model(),
    description="Front door for the php2shell response.",
    instruction=(
        "You help a security engineer respond to the php2shell PHP advisory. "
        "For a question about the advisory, transfer to advisory_analyst. "
        "For a question about one site, transfer to asset_lookup. "
        "For a full response across the fleet, transfer to response_team. "
        "If the request is unclear, ask one short question."
    ),
    # No sampling settings: Gemini 3.8 Flash ignores them, and Google's Gemini 3 guide recommends the defaults.
    sub_agents=[advisory_analyst, asset_lookup, response_team],
)

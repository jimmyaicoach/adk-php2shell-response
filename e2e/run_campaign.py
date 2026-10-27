"""End-to-end test: run the PHP2Shell response campaign against a real model, then check
the path the agents took (trajectory checks) and report the prompt-cache hit rate.

    python -m e2e.run_campaign                 # one full response, implicit caching only
    python -m e2e.run_campaign --runs 3        # three runs, to see the hit rate settle
    python -m e2e.run_campaign --cache         # same, with ADK's explicit context cache

Needs a model: fill in .env first (see .env.example). Exits 1 if any check fails.
Each run's events are saved to reports/e2e-<time>.json for later review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from php2shell_response import core
from php2shell_response.agent import root_agent

APP = "php2shell_response"
USER = "e2e"
DEFAULT_PROMPT = "Run a full response across the fleet."


def flatten(event) -> dict:
    """The parts of one ADK event that the checks and the cache report need."""
    calls, responses, texts = [], [], []
    if event.content and event.content.parts:
        for p in event.content.parts:
            if p.function_call:
                calls.append({"name": p.function_call.name, "args": dict(p.function_call.args or {})})
            if p.function_response:
                responses.append({"name": p.function_response.name, "response": p.function_response.response})
            if p.text:
                texts.append(p.text)
    u = event.usage_metadata
    return {
        "author": event.author,
        "calls": calls,
        "responses": responses,
        "text": "\n".join(texts),
        "state_delta": dict(event.actions.state_delta or {}) if event.actions else {},
        "transfer_to": event.actions.transfer_to_agent if event.actions else None,
        "escalate": bool(event.actions.escalate) if event.actions else False,
        "prompt_tokens": (u.prompt_token_count or 0) if u else 0,
        "cached_tokens": (u.cached_content_token_count or 0) if u else 0,
        "output_tokens": (u.candidates_token_count or 0) if u else 0,
    }


async def run_once(runner: InMemoryRunner, prompt: str) -> tuple[list[dict], dict]:
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    message = types.Content(role="user", parts=[types.Part(text=prompt)])
    trace = []
    async for event in runner.run_async(user_id=USER, session_id=session.id, new_message=message):
        trace.append(flatten(event))
    final = await runner.session_service.get_session(app_name=APP, user_id=USER, session_id=session.id)
    return trace, dict(final.state)


def first(trace, pred) -> int | None:
    return next((i for i, e in enumerate(trace) if pred(e)), None)


def called(name):
    return lambda e: any(c["name"] == name for c in e["calls"])


def responded(name):
    return lambda e: any(r["name"] == name for r in e["responses"])


def check(trace: list[dict], state: dict) -> list[tuple[str, bool, str]]:
    """The trajectory checks from the post, run on one campaign's events."""
    results = []

    t = first(trace, lambda e: e["transfer_to"])
    target = trace[t]["transfer_to"] if t is not None else None
    results.append(("routing: a full response goes to response_team", target == "response_team", f"first transfer: {target}"))

    ra, mv = first(trace, responded("read_advisory")), first(trace, responded("match_versions"))
    results.append(("state: the advisory is read before versions are matched",
                    ra is not None and mv is not None and ra < mv, f"read_advisory at {ra}, match_versions at {mv}"))

    analysts = [first(trace, lambda e, a=a: e["author"] == a) for a in ("exposure_analyst", "patch_analyst")]
    results.append(("order: code matches versions before the analysts start",
                    mv is not None and all(a is not None and a > mv for a in analysts), f"analysts at {analysts}"))

    results.append(("fan out: both analysts ran and saved their own key",
                    all(a is not None for a in analysts) and bool(state.get("exposure_report")) and bool(state.get("patch_report")),
                    f"exposure_report={bool(state.get('exposure_report'))}, patch_report={bool(state.get('patch_report'))}"))

    rounds = sum(1 for e in trace if e["author"] == "planner" and "plan" in e["state_delta"])
    results.append(("loop: one to three rounds", 1 <= rounds <= 3, f"{rounds} round(s)"))

    approvals = [i for i, e in enumerate(trace)
                 if any(r["name"] == "approve_plan" and r["response"].get("status") == "approved" for r in e["responses"])]
    ok_approval = True
    for i in approvals:
        last_plan = max((j for j in range(i) if trace[j]["author"] == "planner" and "plan" in trace[j]["state_delta"]), default=None)
        passed = [j for j in range(i)
                  if any(r["name"] == "check_plan" and r["response"].get("complete") for r in trace[j]["responses"])]
        if last_plan is None or not any(j > last_plan for j in passed):
            ok_approval = False
    results.append(("loop: approval only after check_plan passed on that draft", ok_approval,
                    f"{len(approvals)} approval(s); approved={state.get('approved', False)}"))

    saves = [r["response"] for e in trace for r in e["responses"] if r["name"] == "save_report"]
    path_ok = len(saves) == 1 and str(saves[0].get("path", "")).startswith("reports/")
    results.append(("writes: save_report ran once, inside reports/", path_ok, f"saves: {saves}"))

    results.append(("steps: no step stopped the run", not state.get("halted"), f"halted={state.get('halted', False)}"))

    expected = sorted(s["site"] for s in core.match_fleet(core.load_fleet(), core.load_advisory()) if s["needs_action"])
    got = sorted(s["site"] for s in state.get("affected_sites", []))
    results.append(("facts: the affected sites match the code's answer", got == expected, f"{len(got)} sites"))
    return results


def cache_report(trace: list[dict]) -> dict:
    by_agent: dict[str, dict] = {}
    for e in trace:
        if e["prompt_tokens"]:
            a = by_agent.setdefault(e["author"], {"calls": 0, "prompt": 0, "cached": 0})
            a["calls"] += 1
            a["prompt"] += e["prompt_tokens"]
            a["cached"] += e["cached_tokens"]
    prompt = sum(a["prompt"] for a in by_agent.values())
    cached = sum(a["cached"] for a in by_agent.values())
    return {"model_calls": sum(a["calls"] for a in by_agent.values()), "prompt_tokens": prompt,
            "cached_tokens": cached, "hit_rate": round(cached / prompt, 3) if prompt else 0.0, "by_agent": by_agent}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--cache", action="store_true", help="turn on ADK's explicit context cache for the app")
    args = ap.parse_args()

    # Our chosen threshold: 4,096 tokens, Vertex AI's minimum cache size for most Gemini 3 models
    # (2,048 for Gemini 2). Vertex lists no explicit minimum for 3.8 Flash; its implicit minimum is 6,144.
    cache = ContextCacheConfig(min_tokens=4096, ttl_seconds=600, cache_intervals=10) if args.cache else None
    runner = InMemoryRunner(app=App(name=APP, root_agent=root_agent, context_cache_config=cache))

    all_ok = True
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {"prompt": args.prompt, "explicit_cache": args.cache, "runs": []}
    for n in range(1, args.runs + 1):
        trace, state = await run_once(runner, args.prompt)
        results = check(trace, state)
        cr = cache_report(trace)
        print(f"\n=== run {n} of {args.runs} (explicit cache: {'on' if args.cache else 'off'}) ===")
        for name, ok, detail in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
        print(f"  cache: {cr['cached_tokens']} of {cr['prompt_tokens']} prompt tokens read from cache "
              f"({cr['hit_rate']:.1%}) over {cr['model_calls']} model calls")
        for agent, a in cr["by_agent"].items():
            rate = a["cached"] / a["prompt"] if a["prompt"] else 0
            print(f"    {agent:18} calls={a['calls']:2}  prompt={a['prompt']:6}  cached={a['cached']:6}  ({rate:.0%})")
        all_ok = all_ok and all(ok for _, ok, _ in results)
        record["runs"].append({"checks": [{"name": a, "pass": b, "detail": c} for a, b, c in results],
                               "cache": cr, "trace": trace})

    out = core.REPORTS_DIR / f"e2e-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(f"\nTrace saved: {out.relative_to(core.ROOT)}")
    print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

# PHP2Shell response team: an ADK multi-agent workflow

A critical PHP advisory lands, exploitation attempts are reported within days, and every team asks the same three questions: are we affected, which sites first, and what's the plan. This repo answers them with a team of agents built on Google's [Agent Development Kit (ADK)](https://adk.dev/). It reads the public advisory, checks a fleet of PHP sites, ranks the exposure, and writes a reviewed patch plan for a person to approve.

It's the code behind the post [PHP2Shell response: an AI agent campaign with Google ADK](https://aiboostx.ca/blog/php2shell-response-campaign-with-google-adk/). For the same kind of campaign run as a Claude Code workflow, see [I built an Agent Framework Titan-Kronos](https://aiboostx.ca/blog/claude-code-mythos-fable5/).

![The PHP2Shell response team: an intake agent routes to two quick-answer agents or a sequential response team that reads the advisory, matches versions in code, analyses exposure and patches in parallel, drafts and reviews the plan in a loop, and saves the report](docs/architecture.svg)

## The case: PHP2Shell

In October 2026, two flaws in the PHP interpreter, [CVE-2026-109618 and CVE-2026-109619](https://www.tenable.com/blog/php2shell-cve-2026-109618-cve-2026-109619-frequently-asked-questions-about-critical-php-remote), were disclosed together as "PHP2Shell": unauthenticated remote code execution, rated 9.2 on CVSS v4.0, with fixes released on 21 October in PHP 8.5.12, 8.4.27, 8.3.36 and 8.2.35. [CISA](https://www.cisa.gov/news-events/alerts/2026/10/21/critical-vulnerabilities-php-require-immediate-patching) called for immediate patching. The facts the agents use are in [data/advisory.json](data/advisory.json), each with its source.

The fleet in [data/fleet.csv](data/fleet.csv) is made up: twelve sites across supported, end-of-life and already-patched PHP versions, with owners, exposure and data classes, so every branch of the logic gets exercised.

## How the team works

| Pattern | Agent | What it does |
| --- | --- | --- |
| Routing | `intake` | Sends a question about the advisory to `advisory_analyst`, a question about one site to `asset_lookup`, and a full response to `response_team` |
| Shared state | all | `advisory`, `affected_sites`, `exposure_report`, `patch_report`, `plan` and `feedback` live in session state; every change is an event with its delta |
| Steps in order | `response_team` (SequentialAgent) | Read the advisory, match the fleet, analyse, plan, save |
| Fan out and gather | `analysis` (ParallelAgent) | `exposure_analyst` and `patch_analyst` run at the same time, each saving under its own key |
| A loop with a checker | `plan_room` (LoopAgent) | `planner` drafts, `reviewer` runs `check_plan` in code, and `approve_plan` ends the loop only after it passes; at most three rounds |

The two flows that make it more than a pipeline: [fan out and gather](docs/fanout-flow.svg) and [the review loop](docs/loop-flow.svg).

The code is in [php2shell_response/agent.py](php2shell_response/agent.py). The decisions that must be exact are plain Python in [php2shell_response/core.py](php2shell_response/core.py), covered by [tests/test_core.py](tests/test_core.py).

## Design patterns, with production in mind

- **Facts come from tools, not memory.** The advisory and the fleet are read by tools; the model summarises and plans.
- **Code decides what's affected.** Versions are compared as numbers in `core.status_for`, and the reviewer's coverage check is `core.missing_from_plan`.
- **Each step checks the last.** A `before_agent_callback` on every step stops the run with a clear message if the step before left its output missing.
- **A loop with two exits.** `approve_plan` ends it, and code refuses that call until `check_plan` has passed on the current draft; the cap of three rounds is the other exit, and an unapproved plan is saved as NEEDS-REVIEW.
- **A cache-friendly prompt.** Each response-team agent keeps its fixed rules in `static_instruction`, sent first as the system instruction, and only the changing state in `instruction`, sent after it. The start of every request stays identical, which is what a context cache can reuse; `--cache` in the end-to-end test turns ADK's explicit cache on.
- **Context stays bounded.** Every agent in the response team runs with `include_contents="none"` and works from state, so the third round's prompt is the size of the first.
- **One write, bounded.** The only file the system writes is the plan, in `reports/`, with the folder and name set in code.
- **Read-only, with a person in charge.** No tool touches a server. The plan says a person approves each change, with staging, a canary and a rollback for every site.
- **Traced.** ADK emits OpenTelemetry spans for every agent, model call and tool call, ready to export over OTLP.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in one option
python -m unittest -v     # the plain-code checks; no model needed
adk web                   # open http://127.0.0.1:8000 and pick php2shell_response
```

Try these in the Dev UI:

- "What is PHP2Shell, and which versions fix it?"
- "Is partner-api affected?"
- "Run a full response across the fleet."

The full response writes its plan to `reports/`.

## Test it end to end

With `.env` filled in, run the whole campaign against your model, check the path the agents took, and read the cache hit rate from the API's own token counts:

```bash
python -m e2e.run_campaign                 # one full response
python -m e2e.run_campaign --runs 3        # three runs, to see the hit rate across runs
python -m e2e.run_campaign --runs 3 --cache   # the same, with ADK's explicit context cache
```

It prints a PASS or FAIL line for each trajectory check: the route, the advisory read before versions are matched, code matching before the analysts, both branches saving their keys, one to three loop rounds, approval only after `check_plan` passed on that draft, one save inside `reports/`, no stopped step, and the affected sites matching the code's answer. Then it prints the cached share of prompt tokens per agent, and saves the full trace to `reports/`. It exits 1 if any check fails. `python -m unittest` tests the checks themselves on hand-made traces, with no model.

## Credits

Built on [Google ADK](https://github.com/google/adk-python) and following the patterns in Google's [adk-recipes](https://github.com/google/adk-recipes), both Apache-2.0. The advisory facts come from the public sources listed in [data/advisory.json](data/advisory.json). The fleet is fictional.

## Licence

[Apache-2.0](LICENSE).

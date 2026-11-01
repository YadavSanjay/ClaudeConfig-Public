# AI Harness Engineering — the standard (rev 2026-07)

> Dated standard. Everything here is provisional and expires as models/tooling
> improve. When you revise, copy this file to `standard-<new-date>.md`, edit the
> copy, and update `SKILL.md`'s changelog. Do not overwrite a dated file in place.

Each dimension below has (a) the principle, (b) a checklist you score
`Absent / Partial / Solid`, and (c) the failure mode it prevents. Sources for the
Claude Code specifics are at the bottom.

---

## 1. Context & memory architecture

**Principle:** the agent is amnesiac between context windows; the *filesystem* is
the memory. Never rely on "the model will remember." State that must survive a
compaction or a fresh session lives in files.

Checklist:
- [ ] `CLAUDE.md` exists, is **lean**, and holds only durable conventions, build/
      test commands, and non-negotiables (it costs tokens every turn — no essays).
- [ ] Long-running work has a durable **plan file** (feature list) and a rolling
      **progress file** (lab notes: done / next / blockers) so a fresh session
      rebuilds state from disk, not from a lossy summary.
- [ ] An explicit **compaction policy**: what gets re-injected after `PreCompact`,
      what's safe to drop. Don't leave it to chance on long runs.
- [ ] Skills/prompts load **on demand**, not all-in-context. Progressive disclosure:
      a lean entry file that points at heavier references.
- [ ] Secrets are in a gitignored `.env` / secret store, never in `CLAUDE.md` or
      prompts.

Prevents: context rot, confident re-derivation of your conventions, work lost on
reset, token bloat from an over-stuffed CLAUDE.md.

## 2. Planning (spec before build)

**Principle:** turn a vague ask into a verifiable specification *before* writing
code. The "done" definition must be external and machine-checkable so the agent
can't quietly redefine success mid-run.

Checklist:
- [ ] A **feature/requirements list** with explicit, testable per-item completion
      criteria — each item starts marked *failing/incomplete*.
- [ ] A **scope document** (`VISION.md` or equivalent) the agent can check work
      against and *decline* when something is out of bounds.
- [ ] For big builds: an **initializer** step/prompt that expands the user's
      request into the requirements file (and, for repeatable runs, a boot script).
- [ ] Plan mode / a plan-then-approve step is used for non-trivial work rather than
      one-shotting.

Prevents: premature "done," scope creep, agents guessing requirements.

## 3. Generation / execution

**Principle:** keep the coordinator's context clean; push work into isolated
workers and hold intermediate state outside context.

Checklist:
- [ ] Fan-out work uses **subagents** with their own context/tools that return
      **only a summary** to the main thread (not raw file dumps).
- [ ] Large parallel orchestration uses a **workflow / `ultracode`** script where
      intermediate results live in **script variables**, not the context window;
      concurrency is bounded (up to 16 concurrent subagents).
- [ ] Agents that **mutate files in parallel** run in **worktree isolation** to
      avoid clobbering each other.
- [ ] Each subagent is granted the **minimum tools** it needs.

Prevents: context pollution, lost intermediate state, parallel-write conflicts,
over-privileged agents.

## 4. Evaluation — doer ≠ judge (the highest-value dimension)

**Principle:** the thing that produced the work must not be the thing that
declares it done. Use graduated evaluators, cheapest first.

Three tiers (adopt at least Tier 1 + Tier 2; add Tier 3 for quality-critical work):
- [ ] **Tier 1 — `/goal`:** completion judged after each turn by a *fresh, cheap*
      model (defaults to Haiku), returning yes/no + reason. It is **transcript-only
      (no tools)**, so conditions must be phrased as things the generator's own
      output demonstrates (e.g. "all tests in `test/auth` pass" — the run surfaces
      the result). Prefer quantitative criteria over subjective "good enough."
- [ ] **Tier 2 — deterministic Stop-hook gate:** a script wired to the `Stop`
      event that exits non-zero on failing lint/tests, so the agent *cannot* stop
      with a red build. A guarantee, not an opinion.
- [ ] **Tier 3 — evaluator subagent:** a separate agent with **no Write/Edit
      tools**, a **fresh context that never saw the build**, and a **default-FAIL
      contract** (every criterion starts false; it must open evidence to flip one).
      Because it's a real subagent it *can* run tools (start the server, drive the
      UI, run the suite), overcoming Tier 1's transcript-only limit. Returns
      PASS / NEEDS_WORK with findings; NEEDS_WORK findings seed the next build.
- [ ] **Self-verification skill:** the generator runs an end-to-end check before
      claiming done (e.g. start dev server → exercise the change → screenshot),
      never "the edit applied, therefore it works."

Prevents: models overrating their own work, plausible-but-broken output shipping,
subjective self-grading inflation.

## 5. Deterministic guardrails (hooks)

**Principle:** if it must always happen, wire it — don't ask the model nicely.
Hooks run outside the context window and fire deterministically. (Claude Code
exposes 31 lifecycle events; these three carry most of the weight.)

Checklist:
- [ ] **PreToolUse guard:** block edits to protected paths, reject commands that
      touch secrets, enforce boundaries. It runs *before* the tool, so it can veto.
- [ ] **PostToolUse auto-format / lint:** style becomes a guarantee, not a request.
- [ ] **Stop gate:** deterministic completion check (see Dimension 4, Tier 2).
- [ ] **Brain/hands separation:** credentials are unreachable from the sandbox
      where model-generated code runs; the PreToolUse guard is the enforcement
      point.
- [ ] Hooks are wired in `settings.json` (pointing at scripts that can live
      anywhere) — not assumed to exist as loose files.

Prevents: secret exfiltration, protected-file damage, style drift, unsafe stops.

## 6. Prompt & tool hygiene

**Principle:** prompts and tools are versioned, testable assets — not string
literals sprinkled through code.

Checklist:
- [ ] Prompts live in a **versioned registry** (files + metadata), resolvable and
      diffable, not inlined at call sites.
- [ ] Every external dependency (LLM, vector store, embedder, reranker, HTTP
      source) sits **behind an interface with a test fake**, so the pipeline runs
      in tests with **no network and no heavy models**.
- [ ] LLM outputs that feed code use **schema-validated structured output**
      (validate at the tool/response layer; retry on mismatch).
- [ ] Tool surface per agent is **minimal and explicit**; no blanket `*` grants to
      workers that don't need it.

Prevents: untestable pipelines, prompt drift, brittle output parsing, blast radius
from over-broad tool access.

## 7. Cost tracking & controls

**Principle:** an autonomous loop with no ceiling will run until the budget is
gone. Every long-running or fan-out harness needs *both* accounting and hard caps.

Checklist:
- [ ] **Token / turn caps** on autonomous runs (e.g. a `/goal` token budget, a
      workflow token budget, or a turn limit baked into the condition). A cap is a
      *hard ceiling*, not advisory.
- [ ] **Per-run cost telemetry:** token usage recorded per call and **priced from a
      config** (a pricing table), surfaced in a trace/summary — "what happened and
      what did it cost."
- [ ] **Model tiering:** a cheap default model with **escalation on a quality
      signal** (retry a deteriorated cheap pass on a higher tier, keep-if-better,
      log the upgrade). Don't send everything to the top tier.
- [ ] **Circuit breaker:** cap consecutive failures; on trip, roll back, stop, and
      alert — so a stuck loop can't burn a week's spend in an afternoon.
- [ ] Cost/limit **truncation is logged**, never silent (if you cap coverage at
      top-N, say so).

Prevents: runaway spend, invisible cost, top-tier-for-everything waste,
infinite-retry burn.

## 8. Autonomy safety & recovery

**Principle:** unattended runs need an escape hatch and a watchdog.

Checklist:
- [ ] **Rollback:** `/rewind` (or equivalent snapshotting) restores code /
      conversation to a prior point when a loop goes off-track.
- [ ] **Runaway-process protection:** for unattended loops, a watchdog that detects
      and kills runaway child processes / tight retry loops (platform-specific — see
      the refactor playbook; `/proc` is Linux-only).
- [ ] **Observability / tracing:** long runs emit a trace of decisions, tool calls,
      escalations, and cost — reviewable after the fact.
- [ ] **Explicit unattended-run policy:** what may run scheduled/headless, with what
      caps, and where alerts go.

Prevents: unrecoverable off-track runs, zombie processes, unexplainable outcomes.

## 9. Harness evolution discipline

**Principle:** every harness component encodes an assumption the model *couldn't*
do something. Those assumptions go stale as models improve. The harness doesn't
shrink so much as *move*.

Checklist:
- [ ] After each major model release, **disable harness pieces one at a time** and
      keep only what's still load-bearing (a stronger model may retire your
      context-reset logic, or make a Tier-3 evaluator redundant where a Tier-1
      condition now suffices).
- [ ] The standard is **dated** and re-audited on a cadence.
- [ ] Try **in-product features and custom scaffolding side by side**; keep only
      the load-bearing custom parts.

Prevents: cargo-culted complexity, paying to maintain scaffolding the model no
longer needs.

---

## Verified Claude Code primitive cheat-sheet

Map dimensions → primitives. (Verified against official docs / the Anthropic
reference repo, 2026-07.)

| Need | Primitive | Key property |
|---|---|---|
| Persistent conventions | `CLAUDE.md` | Loads every session; stays in context — keep lean |
| On-demand knowledge | Skills (`SKILL.md`) | Load only when invoked; progressive disclosure |
| Deterministic gates | Hooks (`settings.json`) | 31 lifecycle events; run outside context |
| Isolated workers | Subagents (`.claude/agents/*.md`) | Own context/tools; return only a summary |
| Fresh-model completion check | `/goal <condition>` | Fresh cheap model (Haiku default), transcript-only, yes/no + reason |
| Self-paced / interval reruns | `/loop` | Reruns on interval or self-paced until you stop it |
| Scheduled/unattended runs | `/schedule`, routines | Cron-style cloud agents |
| Large fan-out orchestration | `ultracode` / workflows | JS script, ≤16 concurrent subagents, state in variables |
| Delegate with full context | `/fork` | Background subagent inheriting parent context |
| Rollback | `/rewind` | Restore code / conversation / both to a prior point |

## Anti-patterns (score these as red flags)

- **Generator grades itself** — no doer≠judge separation anywhere.
- **"Please follow the style"** in a prompt where a PostToolUse hook belongs.
- **Unbounded autonomous loop** — no token/turn cap, no circuit breaker.
- **Cost is invisible** — no per-run pricing/usage telemetry.
- **Everything on the top-tier model** — no cheap default + escalation.
- **Prompts as inline string literals** — no registry, undiffable.
- **No test fakes** — pipeline can't run without network/GPU, so it's untested.
- **CLAUDE.md as a dumping ground** — bloated, token-expensive every turn.
- **Silent truncation** — top-N caps or skipped steps not logged.
- **Credentials reachable from the code-execution sandbox** — no brain/hands split.

## Sources (2026-07)

- Anthropic — *Effective harnesses for long-running agents*:
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic — *Harness design for long-running application development*:
  https://www.anthropic.com/engineering/harness-design-long-running-apps
- Anthropic reference repo — `anthropics/cwc-long-running-agents`
  (initializer, `evaluator.md` no-write/default-FAIL, feature-list):
  https://github.com/anthropics/cwc-long-running-agents
- Claude Code docs — `/goal` (Haiku default, transcript-only Stop-hook wrapper):
  https://code.claude.com/docs/en/goal
- Claude Code docs — hooks (31 lifecycle events):
  https://code.claude.com/docs/en/hooks
- Claude Code docs — subagents:
  https://code.claude.com/docs/en/sub-agents

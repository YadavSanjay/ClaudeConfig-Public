# Refactor playbook — bringing a project in line

How to turn the gap report (from the audit workflow) into changes. Read
[standard-2026-10.md](standard-2026-10.md) first for the dimensions.

## Golden rules

1. **Plan before editing.** Present the prioritized plan and get agreement. Harness
   changes touch safety and money.
2. **P0 first, always.** Containment, safety and cost controls come before structural
   tidiness.
3. **One dimension per change set.** Land, verify with the acceptance check, then move
   on.
4. **Prefer in-product primitives over custom code.** `/goal`, hooks, subagents,
   workflows, the sandbox, `--max-budget-usd` and the advisor tool before a hand-rolled
   loop. Keep custom scaffolding only where it is load-bearing and the primitives fall
   short.
5. **Build from failures.** Each new rule, hook or skill names the agent failure it
   prevents and a retire-when condition. Don't install generic skills or subagents
   ahead of need.
6. **Removing is a refactor too.** A stale component (its assumption no longer holds)
   is retired one at a time, with the eval or usage evidence that justified it.
7. **Don't regress test isolation.** Every component that touches an external
   dependency ships with a fake.

## Priority tiers

**P0 — Containment, safety & cost.**
- Unattended or broad-autonomy work runs in a sandbox, devcontainer or VM; egress
  denied by default; credentials outside it; no broad `Bash(*)` allow rules.
- Hard budget on autonomous / headless runs (`--max-budget-usd` or your own gateway
  budget); circuit breaker on consecutive failures.
- Per-run cost telemetry priced from config.
- PreToolUse guard for secrets / protected paths, failing closed.
- A rollback path that covers Bash changes (git), confirmed to work.

**P1 — Verification & evaluation.**
- A runnable check for the main artifact, with evidence shown.
- A deterministic Stop-hook gate; `/goal` conditions for long sessions.
- A calibrated second-opinion reviewer **only** where the work exceeds what the model
  does reliably solo.
- Product evals: deterministic graders, separated capability and regression suites,
  measured noise.

**P2 — Structure, hygiene & pruning.**
- Instruction file under ~200 lines as a table of contents; path-scoped rules; skills
  for procedures; AGENTS.md if several agent tools share the repo.
- Versioned prompt registry; remaining dependencies behind interfaces + fakes;
  schema-validated output; minimal tool surface.
- The same guards in CI; tests for each guard (block and allow).
- Harness usage measurement; retire stale components.

## Sequencing (dependency order)

1. **Instrument before you cap.** Telemetry first, so limits are set from numbers.
2. **Contain and gate before you automate.** Sandbox, caps and the Stop gate exist
   before any `/loop`, routine, schedule or unattended `-p` run.
3. **Fakes before components.**
4. **Registry before escalation.** Version prompts before wiring model tiering.
5. **Test the guard before trusting it.** A hook without a block-and-allow test is
   unverified.

## Platform notes (hooks, watchdogs and sandboxes are OS-specific)

Detect the OS in Phase 1 and generate the right flavor.

- **POSIX (macOS / Linux / WSL2):** `.sh` hook scripts; the Claude Code Bash sandbox
  is available; a watchdog can read `/proc/<pid>/stat` (Linux).
- **Native Windows:** the Bash sandbox does **not** run, so containment for unattended
  work means WSL2, a devcontainer or a VM. Hooks can be Python (stdlib) or PowerShell
  scripts (mind `-NonInteractive`, exit codes, UTF-8, 5.1 quirks: no `&&` / `||`).
  There is no `/proc`: detect runaway children with `Get-CimInstance Win32_Process`
  (filter by `ParentProcessId`) or a Job object; terminate with `Stop-Process`.
- Keep the *logic* identical across platforms; only the shell differs.

## Acceptance checks (prove each item is done)

- Containment → a command that writes outside the allowed tree or reaches a
  non-allowlisted host is blocked; no credential is readable from inside the sandbox.
- Cost telemetry → a real task shows a non-zero priced per-run total.
- Hard budget → a low cap stops the run at the ceiling with a clear message.
- Circuit breaker → injected repeated failures trip it, the run stops, the halt is logged.
- Stop gate → break a test, the agent cannot stop; fix it, it can.
- `/goal` → a "not met" returns a reason and the run continues; "met" clears it.
- Reviewer → has no write tools, returns NEEDS_WORK on a deliberately incomplete
  change, and does not flag style-only issues as blockers.
- PreToolUse guard → a protected edit is vetoed **and** a legitimate edit is allowed;
  a crashing guard blocks rather than allows.
- Test isolation → the suite passes with the network off.
- Pruning → the component's removal leaves the eval or slice result within noise.

## Worked example — an LLM/RAG Python service

A mature RAG service (FastAPI + a vector store + an LLM gateway) often audits like this:

- **Usually already Solid:** interfaces + fakes, a versioned prompt registry, an eval
  suite, per-call usage + pricing config, quality-signal escalation, tracing.
- **Common gaps:**
  - No containment for unattended agent runs (credentials in `.env` on the same box,
    no sandbox on Windows) → run unattended work in WSL2 or a devcontainer.
  - No hard budget on headless agent runs → `--max-budget-usd`.
  - Guards exist but have no block/allow tests, and don't run in CI.
  - Instruction files have grown into design docs → slim to a table of contents.
  - Capability scaffolding (escalation ladders, reasoning caps, decomposition) never
    re-checked against the current model → measure escalation rates and slice results,
    retire what no longer pays.

## After the refactor

- Record the audit and what changed (dated) in the target repo, so the next audit can
  diff against it.
- Schedule the next re-audit: at a cadence floor and within ~2 weeks of a major model
  release.

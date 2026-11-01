---
name: tdd-spec
description: >-
  Test-first workflow with locked, number-based spec tests for any repo with a .claude/tdd/
  pack. Use when starting any new change, feature or bug fix in
  such a repo; when asked to write spec tests, lock tests, implement against locked tests,
  raise a dispute about a test, or verify a change before merge; or by name (/tdd-spec).
  Spec tests are written first from docs/specs/<id>.md, proven red for the right reason, locked,
  and merged by the human before implementation starts. Implementing agents never change them.
---

# tdd-spec: locked spec tests first, then implementation

Design and rationale: [design/plan.md](design/plan.md). Tools: `scripts/tdd.py`
(run as `python <skill>/scripts/tdd.py <command>`, or `python tools/tdd/tdd.py` inside a repo).
Use the py312 env (`conda run -n py312 python ...`, in PowerShell).

A repo takes part when it has `.claude/tdd/profile.toml` on `origin/main`. Its `spec_root` and
`unit_root` (`tests/spec` and `tests/unit` in faultline, `test/spec` and `test/unit` elsewhere)
are written `<spec_root>` and `<unit_root>` below.

## Which role you are in

Your branch decides it. The edit guard enforces this; don't try to work around a denial.

| Branch | Role | You may change | You may not change |
|---|---|---|---|
| `spec/<id>` | spec author | `docs/specs/<id>.md`, `<spec_root>/**` (not `<spec_root>/conftest.py` or `_shared/`), fixtures, **new** stub functions in `src/` | existing functions, `<unit_root>`, locks by hand, maintenance paths |
| `impl/<id>` or `impl/<id>--part<N>` | implementer | `src/`, `<unit_root>/**`, `<dispute_dir>/<id>.md` | anything under `<spec_root>`, `docs/specs/`, locks, locked signatures, maintenance paths |
| anything else | normal work | not the protected paths | spec tests, locks, protected fixtures, maintenance paths |

Maintenance paths (`.claude/tdd/profile.toml`, `protected.txt`, every file directly in
`<spec_root>/` such as `conftest.py`, `<spec_root>/_shared/**`, `tools/tdd/**`, `.github/workflows/**`, `.githooks/**`,
`.claude/settings.json`, `.claude/settings.local.json`) are changed by the human outside Claude,
through a maintenance PR.

Code under `src/` must never refer to the gate (`tdd_plugin`, `tddlib`, `TDD_*` variables) or import
`pytest`, `hypothesis` or `pluggy`; the gate and CI fail on it. That check is a tripwire, not a
barrier: never try to get around it.

On `spec/<id>` you may change only `<id>`'s tests among the *active* locks. Another active change's
locked tests change only on that change's own spec branch, through a re-lock.

## Spec author (`spec/<id>`)

Work from the spec file only, in a fresh context; never from an implementation plan.

1. If `docs/specs/<id>.md` doesn't exist, draft it with these sections:
   - `## Requirement`: what must be true when the change is done;
   - `## Acceptance criteria`: items `- **C1**: <result> within <tolerance>. Oracle: <source>.`,
     each checkable (a value with a tolerance, an invariant, a required refusal or citation);
   - `## Interface`: the new signatures, e.g. `pkg.mod.func(x: float) -> float`;
   - `## Notes`: oracle sources and data snapshots (public-domain or synthetic only).
2. For each new interface item add a stub to `src/`. Its body is only (a docstring and)
   `raise NotImplementedError("tdd:<id>")`. Never change an existing function. Keep the header
   plain: no calls, lambdas or comprehensions in defaults or annotations; decorators only
   `@staticmethod`, `@classmethod`, `@property`, `@abstractmethod`, `@overload`; a new class gets no
   decorators and no calls in its bases or keywords. Imports you add go at module level, name what
   they import (no `*`), and never reuse a name the module already defines.
3. Write tests in `<spec_root>/<id>/`. Every test:
   - is tagged `@pytest.mark.criterion("C1")`;
   - has a `# oracle: <source>` comment naming where the expected value comes from (closed form
     computed in the test, a published example, a hand calculation shown in the test ...); never a
     value obtained by running code;
   - compares floats with `pytest.approx(..., rel=|abs=)` and a one-line reason for the tolerance;
   - reads only committed fixtures, never `data/`, the network or the current date;
   - mocks nothing in the project's own `src` package (fakes only at I/O boundaries);
   - doesn't skip or xfail.

   Each criterion needs at least one point-oracle test **and** one property or metamorphic test
   (`@given(...)` from hypothesis, or `@pytest.mark.metamorphic`). Put fixtures for this spec in
   `<spec_root>/<id>/conftest.py`; spec tests can't see the project's other conftest files.
4. Run `tdd lint <id>` and `tdd red <id>` until both pass:
   - lint is the mechanical rubric;
   - red runs the tests against the stubs (each must fail with an assertion or exactly
     `NotImplementedError("tdd:<id>")`) and against wrong-value stubs (`0`, `NaN`, `None`, empty,
     zero-valued instances), each of which every test must also fail. If the correct answer really
     is one of those values, add `# red-exempt: <variant> <reason>` in the test for review.
5. Run `tdd lock <id>`, commit tests, stubs, spec and `.claude/tdd/locks/<id>.json` together, and
   tell the human the branch is ready for the spec PR. The lock takes effect only once the human
   merges it.

## Implementer (`impl/<id>`)

1. Cut `impl/<id>` from an up-to-date `main` that contains the lock (`tdd status` shows it).
2. Loop: `tdd run <id>`, read the failures, change `src/`, repeat. The Stop gate runs the same
   check when you stop and blocks while tests fail, up to the change's budget.
3. Never edit spec tests, the spec, locks or locked signatures. If you believe a test or its
   oracle is wrong, write `<dispute_dir>/<id>.md` (default `.claude/tdd/disputes/<id>.md`) with
   your reasoning and evidence, and stop. The human decides; a corrected test arrives through a
   re-lock on `main`, after which you rebase.
4. If the gate says "lock for <id> changed on main; rebase", rebase `impl/<id>` onto `origin/main`.
5. When green, run `tdd verify <id>` (fresh hypothesis seeds). A failure is written to the dispute
   file for a re-lock; report it and stop.
6. Before the human merges, post the `gh pr checks <n>` output as a PR comment.

Work too large for one PR goes in `impl/<id>--part1`, `--part2`, ...: each part may leave tests
failing with an assertion or the stub's `NotImplementedError`, never for another reason. The final
PR, from exactly `impl/<id>`, must make every locked test pass; its merge retires the lock.

## If the gate blocks

- **Tampering** ("differ from origin/main or from their lock"): you changed a protected file.
  Restore it with `git restore --source=origin/main -- <path>`.
- **Not passing yet**: fix `src/`. Never loosen a test.
- **Budget used up**: the gate lets you stop. Report what still fails and stop.

## Commands

```
tdd lint <id> | red <id> | lock <id> | run <id> | verify <id>
tdd check            integrity of the working tree against origin/main
tdd gate             what the Stop gate would decide now (dry run)
tdd status           locks, states, warnings
tdd retire <id> --record TEXT
                     retire a lock by record (maintenance branch; see below)
tdd path-guard --base <rev> --head <rev> [--head-ref <branch>]     (CI)
tdd ci [--head-ref <branch>]                                       (CI)
tdd init <repo> [--spec-root test/spec --unit-root test/unit]
                     vendor the tools into <repo>/tools/tdd; add missing pack files and workflows
```

## Retirement by record (human, maintenance PR)

A lock normally retires when the PR from exactly `impl/<id>` is merged into main. When that history
is not in the repository (for example a public mirror published without its development history),
the human retires the lock by record instead: on a maintenance branch, `tdd retire <id> --record
"<why, e.g. the merged PR>"`, committed and merged as a PR of its own. A lock with status `retired`
then behaves exactly like a merge-retired one: its tests are graduated (CI demands they pass), its
signatures and tests are no longer locked. The path guard classifies any lock status leaving
`active` as a maintenance change, so a spec or implementation PR can't retire a lock. Agents never
run `tdd retire`.

## Setting up a repo (human, maintenance PR)

1. `tdd init <repo>` (with `--spec-root test/spec --unit-root test/unit` for XbrlRag and MacroRAG).
   It vendors the tools and adds, where missing, `.claude/tdd/profile.toml`, `protected.txt`,
   `<spec_root>/conftest.py` (hypothesis profiles) and the `tdd-path-guard` and `tdd-spec` workflows.
   Review each, and fill in the project's dependency install step in `tdd-spec.yml`.
2. Add `hypothesis` to the project's requirements and `.claude/tdd/state/` to `.gitignore`.
3. The PR that adds `tdd-path-guard.yml` isn't checked by it: review it by hand.
4. In GitHub settings, disable squash and rebase merging.

## Managed hooks (human, once per machine, elevated PowerShell)

`install.ps1` copies `scripts\*.py` to `C:\Program Files\ClaudeCode\tdd` and writes
`managed-settings.d\tdd.json`: the edit guard (PreToolUse on `Write|Edit|MultiEdit|NotebookEdit`),
the Stop gate and a SessionStart logger. Events go to `$CLAUDE_CONFIG_DIR/tdd-history/<project>/`.
Never run VS Code or `claude` elevated.

Install, and reinstall after any change to `scripts/`. You can't run this yourself (it needs
admin rights), so give the human these steps:

1. Close VS Code and `claude` sessions.
2. Open PowerShell with *Run as administrator* and run
   `powershell -ExecutionPolicy Bypass -File <config>\skills\tdd-spec\install.ps1 -Python <project python.exe> -AcceptWritableSource`.
   The flag acknowledges that the Python env, git and the skill's scripts are user-writable on this
   machine. The human should pass it only when no agent session has run since they reviewed the
   committed scripts. The installer refuses uncommitted script changes.
3. Check the output names the reviewed commit, shows the new version, and `OK: only administrators and SYSTEM can write to
   the managed install`. A `WARNING` that Git or py312 is user-writable is expected on this
   machine (see design/plan.md §10.1).
4. Start VS Code normally.

Remove the hooks with the same command plus `-Uninstall`. Full procedure: design/plan.md §10.1.
Afterwards, check from the new session: `tdd status` shows no version mismatch, and
`C:\Program Files\ClaudeCode\tdd\tddlib.py` has the expected `VERSION`.

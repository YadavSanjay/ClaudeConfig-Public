# tdd-spec: a guide for first-time readers

tdd-spec is a test-first workflow for the faultline, XbrlRag and MacroRAG projects. Before an AI
agent writes any code for a change, tests are written that state the requirement as numbers it
can check. Those tests are then **locked**: the agent that writes the code can run them but can't
change them. This page explains why, how it works, what to watch for, and what isn't built yet.

For the exact rules, see the skill's own files:
- [SKILL.md](../../skills/tdd-spec/SKILL.md) for the operating steps;
- [design/plan.md](../../skills/tdd-spec/design/plan.md) for the full design.

## Why we do this

An AI agent fixing its own code in a loop (run tests, read the failure, change the code, repeat)
is very effective, as long as the tests are honest. Three things can quietly break that:

1. **The tests check the wrong thing.** If the expected value came from running the code itself,
   a passing test proves nothing.
2. **The agent changes the tests.** Under pressure to go green, loosening a tolerance or editing
   an expected value is the easiest fix.
3. **The code is fitted to the tests.** Code that special-cases the exact test inputs passes,
   but is wrong for everything else.

tdd-spec closes each gap:

| Risk | Defence |
|---|---|
| Tests check the wrong thing | Every expected value names an **oracle**, a source independent of the code: a closed-form formula, a published example, a hand calculation. Tests must **fail first** against empty stubs, for the right reason. |
| The agent edits the tests | Tests are **locked** (hashed) once you approve them. Local hooks refuse edits, and CI checks every PR against `main`. |
| Code fitted to the tests | Every criterion also needs a **property test**: a rule that must hold for many generated inputs, not just a few fixed ones. A "wrong-value" check proves the tests catch dummy answers like `0` or `None`. |

**The real guarantee is CI plus your merge.** Only you merge to `main`, and CI checks every PR.
The local hooks exist to catch mistakes early and give the agent fast feedback.

## How it works, in plain terms

Each change, called `<id>`, goes through **two pull requests**:

```
1. spec PR  (branch spec/<id>)                    2. implementation PR  (branch impl/<id>)
   ─────────────────────────────                     ────────────────────────────────────
   write the spec  docs/specs/<id>.md                cut from main, which now has the lock
   write tests + empty stubs                         loop: run tests → fix src/ → repeat
   lint: are the tests well formed?                  the Stop gate won't let the agent stop
   red: do they fail, for the right reason?            while tests fail (up to a budget)
   lock: hash tests and signatures                   verify: re-run property tests, new seeds
   YOU review the oracles and merge  ──────────►     YOU review and merge  ──►  lock retires,
                                                                                tests become
                                                                                normal tests
```

### Step by step

1. **Spec.** A short file states the requirement, numbered acceptance criteria (C1, C2, ...),
   each with a tolerance and an oracle, and the function signatures the tests will call.
2. **Tests and stubs.** A spec-author agent, in a fresh session, writes tests from the spec file
   only. For each new function it adds a *stub* whose body only raises
   `NotImplementedError("tdd:<id>")`.
3. **Checks.**
   - `tdd lint` checks the rules mechanically: every test has a criterion tag and an oracle
     comment, floats are compared with a tolerance, no network or live data, nothing in the
     project's own code is mocked.
   - `tdd red` runs the tests against the stubs (each must fail cleanly), then against stubs
     that return wrong values such as `0`, `NaN`, `None` or empty. Every test must fail against
     each of those too; otherwise it isn't really testing anything.
4. **Lock.** `tdd lock` records hashes of the tests and the signatures. You review the spec PR,
   mainly whether the oracles and tolerances are right, and merge it. The lock counts only once
   it's on `main`.
5. **Implement.** On `impl/<id>`, the agent works until the tests pass. It may add its own unit
   tests, but can't touch the spec tests.
6. **Dispute.** If the agent thinks a test is wrong, it writes a dispute file explaining why and
   **stops**. You decide. A corrected test comes through a new spec PR.
7. **Verify and merge.** `tdd verify` re-runs the property tests with fresh random seeds. CI must
   be green. You merge, and the lock retires: the tests become ordinary regression tests.

### What enforces it

| Layer | Where it runs | What it does |
|---|---|---|
| Edit guard | Every file edit in Claude Code | Refuses edits to locked tests, locks and maintenance files, based on your branch. |
| Stop gate | When the agent tries to finish | On `impl/<id>`, blocks while tests fail, up to a budget (8 attempts by default). On `spec/<id>`, blocks until lint and red pass. |
| CI | Every PR on GitHub | Classifies the PR (spec, implementation, other), refuses mixed PRs, checks locks against `main`, and runs the tests. |
| You | At merge | The final say. Nothing merges without you. |

The local hooks are a **managed install**: admin-owned files under `C:\Program Files\ClaudeCode\`,
so an agent running as you can't switch them off through settings. Installing or updating them
needs an administrator PowerShell, run by you.

### Your branch decides your role

| Branch | Role | May change |
|---|---|---|
| `spec/<id>` | spec author | the spec, the tests for `<id>`, new stubs |
| `impl/<id>` | implementer | `src/`, unit tests, the dispute file |
| anything else | normal work | anything except protected paths |

## When it works well

- **Numbers with an independent answer:** pricing formulas with a closed form (Black-Scholes,
  par bonds, discount factors), risk measures on a small fixed data set, unit and scale handling
  in XBRL facts, retrieval metrics on a fixed corpus.
- **Small, well-specified changes:** one function or a few, with criteria you can state as
  "this value, within this tolerance".
- **Bug fixes:** the spec test reproduces the bug, so it fails first against the current code.

## Edge cases to be aware of

- **The oracle is the hard part.** If no independent source exists, the spec must say so, and you
  consciously accept a weaker check, such as an invariant. Review oracles carefully in the spec
  PR; that's where the value of the whole process is decided.
- **A correct answer that happens to be `0` or empty** trips the wrong-value check. Mark the test
  `# red-exempt: <variant> <reason>`, and you review the exemption.
- **Large changes** can be split into `impl/<id>--part1`, `--part2`, ... Parts may leave tests
  failing cleanly. The final PR, from exactly `impl/<id>`, must make all of them pass.
- **A newer lock on `main`** (after a dispute was fixed) makes the gate say "rebase". Rebase the
  implementation branch onto `main`.
- **Budget used up:** the gate lets the agent stop, records it, and the agent reports what still
  fails. That's a signal for you to look, not a failure of the tool.
- **Merge with a merge commit.** Squash and rebase merging must be disabled in the repo
  settings, otherwise the lock can't detect that the change was merged.
- **A broken Python environment** blocks edits in the tdd repos until it's fixed. That's
  deliberate: failing closed is safer than silently skipping checks.
- **LLM behaviour isn't spec-tested.** In the RAG projects, spec tests cover the deterministic
  parts (parsing, period logic, retrieval on a fixed corpus, citation checks) with a scripted
  fake model. Whether the real model answers well is a separate live evaluation.
- **Code under `src/` must never mention the test machinery** (the gate, pytest, hypothesis).
  A check catches the obvious cases, but code that hides it, for example by building names from
  strings, is left to your review of the diff.

### Accepted limits

- Git Bash, `git.exe` and the Python environment are user-writable on this machine, so a
  determined agent could still disable the *local* hooks. CI and your merge are unaffected.
- Code runs inside the test process, so code that tampers with pytest at run time can't be fully
  prevented. CI and your review are the barrier.
- Copies of a repo that aren't git working trees aren't guarded locally.

## Not implemented yet

- **Few projects use it.** faultline, XbrlRag and MacroRAG have the project pack; nothing outside
  those three has tried it.
- **The CI workflows have never run on GitHub Actions,** only through the local tools. The
  pilot's first PR will be their first real run.
- **`tdd abandon`:** there's no command yet to drop a change cleanly.
- **Shell guard and maintenance mode:** planned for after the pilot, based on what the event
  log shows is needed.
- **The live LLM evaluation runner** for XbrlRag and MacroRAG: designed, not built.
- **A review-core `tdd` lens** to flag code fitted to tests: planned.

## Words used on this page

| Term | Meaning |
|---|---|
| Spec | `docs/specs/<id>.md`: requirement, numbered criteria, interface. |
| Oracle | Where an expected value comes from. It must be independent of the code under test. |
| Stub | A placeholder function that only raises `NotImplementedError("tdd:<id>")`. |
| Right-reason failure | A test failing with an assertion or the stub's exact error, rather than an import error or crash. |
| Property test | A test that checks a rule across many generated inputs, using hypothesis. |
| Lock | `.claude/tdd/locks/<id>.json`: hashes of the tests and signatures. Active from the moment it reaches `main`. |
| Retired / graduated | After the implementation PR merges, the lock retires and its tests become ordinary regression tests. |
| Dispute | A file the implementing agent writes when it believes a test is wrong, before it stops. |
| Maintenance paths | Tooling, CI and settings files that only you change, through a separate PR. |

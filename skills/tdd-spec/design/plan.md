# Design: test-first development with locked, number-based spec tests

Status: **accepted design** (2026-10-03). Phase 1a is built and merged.

Applies to: **faultline**, **XbrlRag**, **MacroRAG**. Another project opts in by adding a project
pack (§8.8) and being added to the managed install (§10.1). In every other project the hooks have
no effect.

## 1. Purpose

Every new change to these projects starts from tests that state the requirement as checkable
numbers, written and approved **before** implementation. The implementing agent then uses those
tests as a self-correction loop: run, read the failure, fix the code, repeat. A green run is
evidence the requirement was met.

That evidence only counts if:

- **the tests are valid**: every expected value comes from a source independent of the code under
  test, and the code under test is not mocked;
- **the tests fail first**, for the right reason;
- **the implementing agent can't change them**, or anything that decides their outcome (test
  config, conftest files, plugins, gate settings, fixtures, CI);
- **the code can't be fitted to the tests**: special-casing the test inputs must not work.

**The guarantee is CI plus your merge.** Only you can merge to `main`, and CI checks every PR
against `main`. The local hooks catch mistakes early and give the agent fast feedback.

### 1.1 Threat model and trusted base

**Adversary.** An agent session running as you, without admin rights. It can write any file you
can, run any command you can (including through scripts), and change its own branch. It can't
push, merge, change GitHub settings or read Actions secrets.

**What each guarantee trusts:**

| Guarantee | Trusted (must not be changeable by the adversary) | Treated as untrusted input |
|---|---|---|
| CI result on a PR | GitHub; `main` (only you merge); the workflows and `tools/tdd/` on `main`; the runner image | Everything in the PR: tests, `src/`, locks, settings |
| Local edit guard and Stop gate | The managed install (scripts, private runtime, settings; admin-only); Git Bash and `git.exe`; the project interpreter for the test run | The working tree, branch names, environment variables, settings files, anything under `src/` |
| Install | The skill's committed scripts and the interpreter it is copied from, at the moment you install | — |

**In scope for the local layer:** any route through Claude Code's file tools, and the common
shell routes, plus anything that would make a local check silently pass where CI would fail.

**Out of scope for the local layer** (CI and your review cover these):
- tampering with Git Bash, `git.exe` or the project interpreter while they are user-writable
  (§10.1);
- code in `src/` that tampers with the test process at run time in disguised ways (§9.2);
- copies of the repo that aren't git working trees.

**Stopping rule for reviews.** Fix P0 and P1 findings, and P2 findings that are in scope above.
Record out-of-scope findings as accepted risk in review-history, with a pointer to this section,
rather than reopening the design.

## 2. Starting point (2026-10-03)

| | faultline | XbrlRag | MacroRAG |
|---|---|---|---|
| Test root | `tests/` (by subsystem) | `test/` (`unit/`, `integration/`, `eval/`) | `test/` (flat) |
| Pytest config | `pyproject.toml` | `pytest.ini` | none |
| Project hooks | none | guard, format, Stop gate | env guard, format, Stop gate |
| CI | `pricer-ci.yml`, manual only (needs gitignored `data/marketdata`) | runs `test/unit` | runs `test/` |
| Existing numeric oracles to build on | real-data bond tests (UST10Y vs par, FINRA TRACE, Z-spread) | `test/eval/golden/xbrl_golden_v1.json` with `rel_tol`, numeric grader | exact retrieval metrics (hit@k, recall@k, MRR) |

All three repos are private on a free GitHub plan, so CI checks can't be made required for
merging. The py312 env has pytest 9.0.1 and no `hypothesis`.

## 3. Components

| Component | Location | Content |
|---|---|---|
| Policy | Global `ClaudeConfig/CLAUDE.md`, about 8 lines | New changes follow this workflow; protected paths follow §7; never bypass git hooks; use the `tdd-spec` skill |
| Skill | `ClaudeConfig/skills/tdd-spec/` | Workflow, rubric, dispute procedure, and the source of the tools: `spec_lint`, `red_check`, `lock`, `verify`, `gate`, `path_guard`, plus the shared outcome classifier and signature code |
| Vendored tools | `<repo>/tools/tdd/` (`tdd.py`, `tddlib.py`, `tdd_plugin.py`, `MANIFEST.json` with version and hashes) | The pinned copy CI uses; `tdd init` writes it and any missing pack files and workflows |
| Managed install | `C:\Program Files\ClaudeCode\` (admin-owned) | Hook settings in `managed-settings.d/`, the installed hook scripts and gate |
| Project pack | `<repo>/.claude/tdd/` | `profile.toml`, `oracles.md`, `protected.txt`, `locks/`, local `state/` |
| CI | `<repo>/.github/workflows/tdd-path-guard.yml`, `tdd-spec.yml` | Path classification; spec and regression tests |
| Event log | `ClaudeConfig/tdd-history/<project>/events.jsonl` | Hook and gate events |

Each project's root `CLAUDE.md` has one line pointing to `.claude/tdd/` and stating the test
layout.

## 4. Terms

- **`<id>`**: the change identifier, used in branch, spec, test and lock names.
- **`<spec_root>`, `<unit_root>`, `<dispute_dir>`**: set in `profile.toml`. faultline uses
  `tests/spec` and `tests/unit`; XbrlRag and MacroRAG use `test/spec` and `test/unit`.
- **Branches**: `spec/<id>` for test authoring; `impl/<id>` for implementation; `impl/<id>--part<N>`
  for an implementation split over several PRs.
- **Active lock**: `.claude/tdd/locks/<id>.json` on `origin/main` with status `active`, not yet
  retired.
- **Graduated tests**: spec tests whose lock has retired. They are ordinary regression tests.
- **Maintenance paths**: `.claude/tdd/profile.toml`, `.claude/tdd/protected.txt`,
  every file directly in `<spec_root>/` (such as `conftest.py`, or a helper module, since the spec
  root is on the test run's `sys.path`), `<spec_root>/_shared/**`, `tools/tdd/**`, `.github/workflows/**`,
  `.githooks/**`, and `.claude/settings.json` / `.claude/settings.local.json` (an `env` block there
  reaches the hooks' environment).
- **Right-reason failure**: a test failing with an assertion (`AssertionError`, or pytest's own
  failures such as `pytest.raises` reporting "DID NOT RAISE"), or with exactly
  `NotImplementedError("tdd:<id>")`. Anything else (another exception, import or fixture error,
  skip, xfail, missing test) is a **wrong-reason** failure.

## 5. Workflow

Each change uses two PRs: a **spec PR** and an **implementation PR**.

1. **Spec.** `docs/specs/<id>.md` contains:
   - the **requirement**;
   - **acceptance criteria**, each a checkable result: a value with a tolerance, an invariant, a
     required refusal or citation;
   - the **interface**: signatures the tests will call (module, name, parameters with annotations,
     return type).

   You write it, or the spec-author agent drafts it and you approve it in the spec PR.
2. **Tests.** A spec-author agent, in a fresh context, works on `spec/<id>` from the spec file only.
   It writes:
   - tests in `<spec_root>/<id>/`, each tagged with its criterion and an `# oracle: <source>`
     comment;
   - a stub for each new interface item, whose body only raises `NotImplementedError("tdd:<id>")`.
     Stubs may be added to existing modules. Existing functions are never changed. A stub's header
     runs at import time, so it may hold only names, attributes, subscripts and constants:
     - no calls, lambdas or comprehensions in defaults or annotations;
     - decorators only from `staticmethod`, `classmethod`, `property`, `abstractmethod`, `overload`;
     - a new class has no decorators and no calls in its bases or keywords;
     - added imports are module-level, never `*`, and never rebind a name the module already
       defines or the PR adds (that would change behaviour with every body unchanged).

   Spec tests are self-contained. Fixtures for one spec go in `<spec_root>/<id>/conftest.py`.
   Fixtures shared between specs go in `<spec_root>/conftest.py` or `<spec_root>/_shared/`, which
   are maintenance paths.
3. **Checks.** `spec_lint` (§8.2) and `red_check` must pass. `red_check` runs two passes:
   1. **Real stubs.** Every test must fail for the right reason.
   2. **Wrong-value stubs.** Every interface item a test uses is switched, all together, to the
      same variant from the table below. Every test must fail (any exception counts) against every
      variant.

      | Return type | Variants |
      |---|---|
      | numeric | `0`, `NaN`, `None` |
      | `str` | `""`, `None` |
      | list, tuple, dict, set, DataFrame, Series, ndarray | empty instance, `None` |
      | dataclass or pydantic model | instance with zero-valued fields (two levels deep), `None` |
      | other class, or no annotation | `None` |
      | `None` (procedure) | returns without doing anything |

      A test whose correct answer is one of these values carries `# red-exempt: <variant> <reason>`,
      which you review.

   For a bug fix to existing code (no stubs), only pass 1 runs, against the current code, and each
   test must fail with `AssertionError`.
4. **Lock.** `tdd lock <id>` writes the lock file (§9.1) on `spec/<id>`. You review the spec PR,
   focusing on whether the oracles and tolerances are right, and merge it. **The lock takes effect
   only once it is on `main`.**
5. **Implement.** The implementing agent works on `impl/<id>`, cut from `main` after the lock
   merged. It may read the spec tests, but can't change any protected path or locked signature. It
   loops (run, read failure, fix `src/`) under the Stop gate (§9.4), and may add its own tests in
   `<unit_root>/`.
6. **Dispute.** If the agent believes a test or oracle is wrong, it writes
   `<dispute_dir>/<id>.md` with its reasoning and evidence, and stops. You decide. A corrected test
   goes through a re-lock spec PR from a fresh `spec/<id>`. The impl branch is then rebased onto
   `main`.
7. **Verify, before you merge the implementation PR:**
   - CI is green (§11). The agent posts the `gh pr checks` output as a PR comment.
   - `verify` reruns the property tests with a random seed and more examples. If it finds a failure,
     it writes the falsifying example and seed into `<dispute_dir>/<id>.md`. A re-lock then adds
     the example to the locked test as `@example(...)`.
   - review-core runs with its **tdd lens** (advisory). It looks for hard-coded expected values,
     branches on test inputs, test-environment detection, and monkeypatching of production code.
   - For RAG changes, the live LLM eval runs (§8.6).

Bug fixes follow the same cycle; the spec test reproduces the bug.

## 6. Lifecycle

| State | Source of truth | Meaning |
|---|---|---|
| authored | local cache | Tests, stubs and lock on `spec/<id>`, passing `spec_lint` and `red_check` |
| locked | lock on `origin/main`, status `active` | Spec PR merged; tests red by design |
| implementing | branch `impl/<id>` or `impl/<id>--part<N>` containing the lock | Work in progress |
| green | recomputed by the gate on every check | All of `<id>`'s tests pass and integrity holds |
| disputed | `<dispute_dir>/<id>.md` on the impl branch | Agent has stopped pending your decision |
| budget-exhausted | local cache and event log | Iteration budget used up (§12.2) |
| retired | the PR from `impl/<id>` is merged into `origin/main`, or lock status `retired` on `origin/main` (see Retirement) | Tests graduated |
| abandoned | lock status `abandoned` on `origin/main` | Change dropped |

The local cache is `.claude/tdd/state/<id>.json` (gitignored). The gate never trusts it for
`green`.

**Retirement.** A lock retires when the PR from exactly `impl/<id>` is merged into `main`. Any
one of three signals proves it, and none depends on the merge title alone:

1. a merge commit on `origin/main` whose subject is GitHub's default,
   `Merge pull request #<N> from <owner>/impl/<id>` (offline);
2. a merge commit on `origin/main` whose second parent is the tip of `origin/impl/<id>`. This
   works offline and with any title, as long as the branch hasn't been deleted and pruned;
3. GitHub's record of a merged PR whose head branch is exactly `impl/<id>`, with its merge commit
   on `origin/main`. This works with any title, even after the branch is deleted. It is read with
   `gh api` (the local token, or `GH_TOKEN` in CI).

A `--part<N>` branch matches none of them. A positive result is cached in the state folder and
re-checked against `main` before use; a negative GitHub answer is cached for 10 minutes. Any
failure, such as being offline or `gh` missing, counts as "not known", so the lock stays active,
which is the safe direction.

Every PR must be merged with a merge commit; squash and rebase merging are disabled in the repo
settings (§13). If an active lock's stubs are gone from `origin/main` but no signal finds the
merge, `tdd status` and the gate warn.

**Retirement by record** (0.4.1). When the merge isn't in this repository's history (for example a
public mirror published as a single commit), the human runs `tdd retire <id> --record "<why>"` on a
maintenance branch. It sets the lock's status to `retired` with the record and the date, and the
lock then counts as retired exactly like a merged one: the status is read from `origin/main`, never
from the PR. To keep it a human decision, the path guard classifies any lock whose status leaves
`active` (or changes from main's non-active status) as a maintenance file, so the PR is a
maintenance PR that may change nothing else. `tdd retire` refuses to run on `spec/*` and `impl/*`.

**Graduated tests** stay in `<spec_root>/<id>/` and run in CI on every PR and every push to `main`,
where they must pass. They have no lock checks. Only spec PRs may change them.

**Part PRs.** An implementation split over `impl/<id>--part<N>` PRs merges into `main` while the
lock is active. Each part only needs to avoid wrong-reason failures. The final PR, from `impl/<id>`,
must make every test of the lock pass.

**Abandoning.** A spec PR sets the lock's status to `abandoned` and removes `<spec_root>/<id>/`.
After that merges, an ordinary implementation PR removes the stubs or any code already merged.

**Refactors** need no spec. A refactor is an implementation PR: it can't touch `<spec_root>/**`,
and all graduated spec tests and unit tests must still pass.

## 7. Permissions

### 7.1 PR kinds

`tdd-path-guard` classifies each PR by the files it changes. A PR that mixes kinds fails.

| PR kind | May change | May not change |
|---|---|---|
| Spec | `docs/specs/<id>.md`; `<spec_root>/**` except maintenance paths; `locks/<id>.json`; spec fixtures; added stub-only functions in `src/` | Other `src/` code; existing signatures; maintenance paths |
| Maintenance | Maintenance paths only | Anything else |
| Implementation | `src/`, `<unit_root>/**`, other non-protected tests, docs | `<spec_root>/**`; locks; signatures in an active lock; maintenance paths |

### 7.2 Who may do what

The edit guard applies this table from the branch of the repo that contains the edited file.
Edit rights tied to a branch are safe because nothing on a branch counts until you merge it.

| Operation | Spec-author agent (`spec/*`) | Implementing agent (`impl/*`) | You |
|---|---|---|---|
| Edit `docs/specs/<id>.md` | Draft | No | Yes |
| Edit `<spec_root>/**` except maintenance paths, including graduated tests and fixtures, but not another active change's locked tests | Yes | No | Yes |
| Add stub-only interface items to `src/` | Yes | — | Yes |
| Write `locks/<id>.json` | Only through `tdd lock` | No | Yes |
| Write `<dispute_dir>/<id>.md` | No | Yes | Yes |
| Change `src/` beyond adding stubs | No | Yes, except locked signatures | Yes |
| Write `<unit_root>/**` | No | Yes | Yes |
| Maintenance paths | No | No | Outside Claude, or in maintenance mode (§10.6) |
| `C:\Program Files\ClaudeCode\**` | No | No | Yes, with admin rights |
| `ClaudeConfig/tdd-history/**` | Hooks append only | Hooks append only | Yes |
| `.claude/tdd/state/**` | Gate only | Gate only | Yes |
| `.git/**` through file tools | No | No | Yes |
| Merge a PR | No | No | Yes |

On any other branch, every protected path is denied to agents. Protected paths are the spec root,
locks, maintenance paths, `protected.txt` entries, `tdd-history/**` and `.git/**`.

## 8. Test rules

### 8.1 Rules

- **Independent oracle** (§8.3). Never a value produced by running the new code.
- **Stated tolerance.** Every float comparison has an `abs` or `rel` tolerance and a one-line
  reason.
- **Pinned inputs.** Spec tests use only committed fixtures or content-addressed snapshots. They
  never use the live `data/` tree, the network or the current date; "today" is a fixed valuation
  date.
- **No skipping.** Missing data or a missing dependency is a failure.
- **Fakes only at I/O boundaries** (network, LLM provider, S3, external DB), behind a `base.py`
  interface. The code under test, its numerics and its data transforms are never mocked.
- **Self-contained** (§5, step 2).
- **Coverage.** Each criterion has at least one point-oracle test and at least one property or
  metamorphic test. The lock records the mapping.

### 8.2 `spec_lint`

| Check | Method |
|---|---|
| Float `==` without `approx` / `isclose` / `assert_allclose` | AST |
| Test without an `# oracle:` tag | AST and comments |
| Criterion without a point test or a property test | Markers against the spec's criteria |
| `skip`, `skipif`, `xfail`, `importorskip` | AST |
| `mock.patch` / `monkeypatch.setattr` targeting the project's `src` package | AST |
| Use of `data/`, network clients, `date.today()`, `datetime.now()`, `time.time()` | AST and imports |
| A stub body other than docstring + `raise NotImplementedError("tdd:<id>")`, or a changed existing function | AST against `main` |
| Per-test hypothesis settings overriding `derandomize` or `database` | AST |
| `# red-exempt:` tags | Listed for your review |

### 8.3 Oracles and what they prove

| Oracle | Proves | Does not prove |
|---|---|---|
| Closed form computed in the test with numpy/scipy (not QuantLib) | The numerics, independently | Conventions the formula ignores |
| Published worked example or standard test grid (e.g. ISDA CDS standard model) | Numerics and conventions against an outside reference | Cases outside the grid |
| Hand calculation shown in the test | That case, to the shown precision | Other cases |
| QuantLib test-suite values, for a QuantLib-based pricer | Wiring: day counts, calendars, settlement, argument order | QuantLib's numerics |
| Analytic Greek vs bump-and-reprice with the same pricer | The Greek code is consistent with the pricer | That the pricer is right |
| Invariants (monotonicity, convexity, parity, no-arbitrage) | Necessary conditions | Correct levels |
| Market reconciliation against a dated snapshot | Realism within quote noise | Precision |

Each criterion needs an oracle from the first three rows where one exists. Otherwise the spec says
so, and you accept the weaker oracle at lock time.

### 8.4 faultline

- **Closed form:** Black-Scholes prices and Greeks, put-call parity, par bond yield equals coupon,
  zero-coupon discount factors, forwards from discount factors.
- **Outside references:** ISDA CDS standard model grid, published worked examples.
- **Wiring:** QuantLib test-suite values.
- **Greek consistency:** bump-and-reprice, with bump size and tolerance stated together.
- **Invariants:** price falls as yield rises, positive convexity for option-free bonds,
  no-arbitrage bounds, P&L explain closes to zero.
- **Risk aggregates:** historical VaR and ES on a small committed return series, against a hand
  calculation.
- **Snapshots:** public-domain or freely redistributable sources (e.g. U.S. Treasury curves) or
  synthetic data only. FINRA TRACE, ICE and DTCC data are not committed unless their terms allow it.
  Reconciliation tests against such data stay outside the gate.

### 8.5 XbrlRag and MacroRAG

Spec tests cover the **deterministic** layers:
- parsing and normalisation: XBRL facts, unit and scale, sign, currency;
- period logic: fiscal vs calendar year, fiscal calendar mapping, restatements;
- derived metrics, against a hand calculation;
- retrieval on a committed fixture corpus: expected chunk ids in the top k; hit@k, recall@k and
  MRR at set values;
- code-level validators: citation (the cited fact resolves and matches the answer within
  `rel_tol`), number grounding (every number in the answer traces to a retrieved fact), and the
  refusal decision where code makes it.

Whether the LLM itself refuses, stays grounded or gives the right number is tested by the live eval
(§8.6).

### 8.6 LLM behaviour

- **Spec tests** use a scripted fake provider keyed by **case id**, returning a fixed structured
  output per case, and test what the code does with it. They don't depend on prompt text, and
  prompt files aren't locked.
- **Unit tests** may replay recorded responses keyed by a hash of model, normalised prompt and
  parameters. A miss fails loudly with the command to re-record.
- **The live eval** runs on demand before merge (by you or a manual CI job), never in the Stop
  gate:
  - Each case runs `r` times (default 3) and passes only if every run passes. Per-run hard
    requirements (citation present, refusal where required) are checked by the code-level
    validators.
  - A criterion is judged on the 95% Wilson lower bound of the case pass rate over `n` cases.
  - Default: 40 cases × 3 runs, target 0.85. 40/40 gives 0.91 and 39/40 gives 0.87, so exactly one
    failing case is allowed. A spec may set other values, but its target can't exceed what its `n`
    can reach (for all cases passing, n / (n + 3.84)).

### 8.7 Defences against fitting the code to the tests

- **Property and metamorphic tests**, required per criterion:
  - faultline: parity over generated strikes and maturities; homogeneity in spot and strike;
    monotonicity in vol and maturity; price → yield → price round trip.
  - RAG: rescaled fixture units give the same normalised value; distractor documents don't change
    the answer; chunk order doesn't change the retrieved set.
- **hypothesis** is added to each project's requirements. The gate uses a profile registered in
  `<spec_root>/conftest.py` with `derandomize=True` and `database=None`. `verify` uses a random seed
  and more examples.
- **Wrong-value red check** (§5, step 3).
- **tdd lens** in review-core (advisory).

### 8.8 Project pack

```
.claude/tdd/
  profile.toml      spec_root, unit_root, dispute_dir, PYTHONPATH, gate plugins (-p names),
                    hypothesis profiles, budget, valuation date
  oracles.md        oracle catalogue for the project (§8.4 or §8.5)
  protected.txt     always-protected globs: golden sets, shared fixtures
  locks/<id>.json   one per active or abandoned change
  state/            gitignored local cache
```

## 9. Lock and gate

### 9.1 Lock contents

- sha256 of every file under `<spec_root>/<id>/`: tests, fixtures, snapshots, its `conftest.py`;
- **interface signatures**: module path, qualified name and normalised AST signature (parameter
  names, kinds, defaults, annotations, return annotation, decorators) of each interface item.
  Function bodies and other code in the module aren't locked;
- **absence records**: `conftest.py` in each directory from the repo root down to
  `<spec_root>/<id>/` where none exists, and `sitecustomize.py` / `usercustomize.py` at the repo
  root and on the gate's `PYTHONPATH`;
- the criteria → node id mapping, and the full list of node ids;
- the spec file's hash, the tool version and the status.

### 9.2 Trust anchor

The gate reads locks and `profile.toml` from `origin/main`
(`git show origin/main:<path>`), never from the working tree. It runs `git fetch origin main` first
(10 s timeout); if that fails, it uses the existing ref and reports its age.

A lock applies to a branch only if the lock's commit on `origin/main` is an ancestor of `HEAD`.
Older branches without the lock aren't reported as tampering. If `origin/main` has a newer version
of the current change's lock than the branch contains, the gate blocks with "lock for `<id>`
changed on `main`; rebase `impl/<id>`".

**Integrity check** means:
- for each applicable active lock, file hashes, signatures and absence records match;
- `protected.txt` paths match `origin/main`, gitignored files included: ignoring a file
  (`.gitignore`, `.git/info/exclude`) doesn't hide it from the comparison;
- project code doesn't visibly reach into the gate. Code under `src/` runs inside the test
  process, so it could tamper with the gate's report in ways no isolation prevents. This check is
  a **tripwire, not a barrier**: `src/` must not name the gate's modules or variables
  (`tdd_plugin`, `tddlib`, `TDD_REPORT`, …), and must not import `pytest`, `_pytest`, `hypothesis`
  or `pluggy`, including through `__import__` or `importlib` with a literal name. Code that builds
  those names from strings gets past it; your review of the implementation diff is the barrier.

### 9.3 Controlled test run

The gate and CI run spec tests the same way:

- environment reduced to OS essentials (`PATH`, `SYSTEMROOT`, `TEMP`/`TMP`, …) and
  `PYTHONIOENCODING`; every other `PYTHON*` and `PYTEST*` variable is dropped;
- **an isolated interpreter**: `python -I`, so neither the current directory nor `PYTHONPATH` is
  on `sys.path`. A small bootstrap then appends, *after* the standard library and site-packages:
  the tools directory, the profile's `pythonpath`, and the spec root (for `_shared` helpers). A
  project module can't shadow pytest, the standard library or the gate's own plugin, because
  each of those is found first;
- `-c <the gate's own pytest config>`, built into the tools (which come from the managed install
  or from `main`), so the repo's own pytest config is ignored;
- `--rootdir=<repo root>`, `--confcutdir=<repo root>/<spec_root>` and `--import-mode=importlib`
  (pytest doesn't add test directories to `sys.path`);
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, with `-p` for each plugin listed in `profile.toml`;
- `-p tdd_plugin --hypothesis-profile=<gate profile> --strict-markers -p no:cacheprovider`.

`tdd_plugin` is the gate's own pytest plugin. It records the collected node ids and, for each
test, the outcome and the exact exception (type, arguments, group members, and whether it is an
assertion). It also applies the wrong-value variants for `red_check`.

Then:
- **signature check**: each locked signature on `HEAD` matches the lock;
- **node id check**: the collected node ids equal the lock's list;
- **outcome check**: each result is classified as pass, right-reason failure or wrong-reason
  failure. When hypothesis groups several failures (`ExceptionGroup`, `FlakyFailure`), the group
  counts as right-reason only if every member does. `red_check`, the gate and CI share this
  classifier.

### 9.4 Stop gate behaviour

The Stop gate runs for the repo the session was started in.

| Situation | Gate does | Blocks the stop |
|---|---|---|
| Repo has no `.claude/tdd/` | Nothing | Never |
| Other branch | Integrity check; warns about unrecognised retirements | Only on tampering |
| `spec/<id>` | `spec_lint` and `red_check` | On failure, until the budget is used |
| `impl/<id>` | Integrity check, then the controlled run of `<id>`'s tests | While any test fails, until the budget is used |
| `impl/<id>--part<N>` | Same | On wrong-reason failures, until the budget is used |
| impl branch with a dispute file | Integrity check | Only on tampering |
| Newer lock on `main` than on the branch | Reports "rebase" | Yes, counted toward the budget |
| Tampering on any branch | Reports the paths and how to restore them | Up to 3 times in a row, then allows the stop and logs a critical event |

When the budget runs out, the gate allows the stop, marks the change `budget-exhausted` and logs
it. It ignores `stop_hook_active`; the budget is its loop guard. Budget and status are kept per
role (`spec-<id>`, `impl-<id>`), so a spec author who uses up the budget doesn't switch off the
implementer's gate.

## 10. Hooks

### 10.1 Managed install

The hooks are installed in `C:\Program Files\ClaudeCode\`, which needs admin rights to change:

- `managed-settings.d/tdd.json`: the hook definitions;
- `tdd\`: the edit guard, Stop gate, SessionStart logger and gate code;
- `tdd\python\`: a private interpreter that runs the hooks. It is a copy of the project
  interpreter's `python.exe`, DLLs and standard library, without site-packages (about 36 MB).
  The hooks use only the standard library. The project's interpreter, which any user can
  modify, runs only the spec tests.

The tdd repo names (`faultline`, `xbrlrag`, `macrorag`, kept in `hooks.py`) are written into the edit guard's command at install time. The command
never reads a names file at run time: a names file with Windows line endings made the match fail,
and the guard failed open.

Managed settings can't be overridden by user, project or local settings. The hook scripts sit in
the same directory, so they are as protected as the settings. The skill provides an install script
that you run as administrator, and `tdd status` reports when the installed version differs from
the skill's.

Hooks run the private interpreter by absolute path with `-I -S`, so `PYTHON*` variables, the user
site directory, `.pth` files and `sitecustomize` can't affect them.

The hooks also drop the tools' test-only variables (`TDD_PYTHON`, `TDD_HISTORY_DIR`,
`TDD_MANAGED_DIR`) from their environment. Each hook command has these written in at install time,
never read from the environment:
- `--history-dir`: the event log folder;
- `--git`: the absolute path of `git.exe`;
- `--test-python`: the project interpreter for the spec tests.

**Trust boundary.** The installer fails unless only administrators and SYSTEM can write to the
install: scripts, runtime and settings. Three things outside it are still in the local boundary:
- the Git Bash that Claude Code runs hook commands with;
- the `git.exe` the hooks call;
- the project interpreter the tests run on.

On the machine this was built on, all three are user-writable, and the installer warns about each. Until Git for Windows is installed
under `C:\Program Files\Git` and the installer rerun with `-Git` pointing there, the local hooks
catch mistakes and casual workarounds but are not tamper-proof. CI and your merge stay the
guarantee.

**Never run VS Code or `claude` elevated.** An elevated session gives the agent admin rights over
the managed install.

**Installing, updating and removing.** The same command installs the hooks and updates them,
replacing whatever was installed before. Run it after first setup, and again whenever `scripts/`
changes (a new tools version).

Running elevated means trusting every input, so before it executes or copies anything, the
installer checks with PowerShell alone which sources non-admins can modify: the interpreter and
its standard library, `git.exe`, and the skill's scripts. If any can be, it installs nothing
unless you pass `-AcceptWritableSource`. Only pass it when no agent session has run since you
last reviewed those sources. On this machine all of them are user-writable, so the flag is
needed every time.

It then refuses uncommitted changes to `skills/tdd-spec/scripts` and prints the commit it
installs. It copies the private runtime with PowerShell, so the project interpreter is never
run elevated. At the end it prints the SHA256 of each installed script.

1. Close VS Code and any `claude` terminals. Hooks are loaded when a session starts, so an open
   session keeps the old ones.
2. Open PowerShell as administrator: Start → type `PowerShell` → right-click → *Run as
   administrator*.
3. Run:
   ```
   powershell -ExecutionPolicy Bypass -File <config>\skills\tdd-spec\install.ps1 -Python <project python.exe> -AcceptWritableSource
   ```
   Optional parameters:
   - `-Python <project python.exe>` (required): runs the
     spec tests, and the private runtime is copied from it;
   - `-Git <git.exe>` (default: the `git` on PATH);
   - `-Gh <gh.exe>` (default: the `gh` on PATH; optional): lets the gate recognise a merged
     `impl/<id>` PR whose branch was deleted;
   - `-HistoryDir <event log folder>` (default `$CLAUDE_CONFIG_DIR\tdd-history`);
   - `-Target <install folder>` (default `C:\Program Files\ClaudeCode`).
4. Check the output:
   - `Installing scripts as of commit: <hash> <subject>` names the commit you reviewed;
   - `Installed tdd-spec <version> hooks` with the expected version;
   - the `runtime:`, `events:`, `git:` and `tests on:` paths you expect;
   - `OK: only administrators and SYSTEM can write to the managed install`.

   A `WARNING` about Git or the project interpreter is the trust-boundary note above. `FAILED`
   or an error means the install must not be used.
5. Close the admin window, and start VS Code normally (not elevated).
6. Check it from a normal session:
   - `tdd status` no longer reports a version mismatch;
   - an edit to a locked spec test on an `impl/*` branch is denied with "tdd edit guard".

To remove the hooks, run the same command with `-Uninstall` from an admin PowerShell. It deletes
`managed-settings.d\tdd.json` and the `tdd` folder.

### 10.2 Edit guard (PreToolUse)

**Matcher:** every file-writing tool, at least `Write|Edit|MultiEdit|NotebookEdit`.

**Decision.** The guard resolves `file_path` (`notebook_path` for `NotebookEdit`) with
`os.path.realpath`, which follows `..` and junctions, and walks up to the git toplevel, including
a worktree whose `.git` is a file. If that repo has `.claude/tdd/`, the guard applies §7.2 using
that repo's branch, locks and `protected.txt`. The session's working directory plays no part, so
edits from a session started elsewhere, and edits in worktrees or clones at any path, are guarded.
A denial is returned as PreToolUse JSON.

On Windows, paths are compared case-insensitively. `realpath` corrects the case only of folders
and files that already exist, and NTFS treats `.CLAUDE/Settings.json` as `.claude/settings.json`.
The CI path guard also classifies case variants as the protected path, because the repo may be
checked out on Windows.

**When Python runs.** Python starts on every file edit, so the decision always uses the real
path. That covers junctions and worktrees in folders with any name. If Python fails, the edit
is denied only when the hook input names a tdd repo:

```
IFS= read -r -d '' in; shopt -s nocasematch; re='(faultline|xbrlrag|macrorag)'; "<runtime>" -I -S "<managed>/hooks.py" edit --history-dir "…" --git "…" --test-python "…" <<<"$in" && exit 0; [[ $in =~ $re ]] && { echo "tdd edit guard failed; edit to a tdd repo blocked" >&2; exit 2; }; exit 0
```

The command uses only bash builtins, because under Git Bash on Windows every pipe, `$(...)` or
external command costs a process start. Measured on this machine, against a no-op hook of about
210 ms:
- an edit outside the tdd repos costs about 180 ms more;
- an edit inside one costs about 460 ms more, mostly two `git` calls.

A prefilter that started Python only when the path named a tdd repo was faster. It was dropped
because a junction or an unnamed worktree went straight past it.

A broken runtime therefore blocks edits to the tdd repos and nothing else. Shell commands, including
`conda` repairs, keep working.

### 10.3 Stop gate

It runs for the session's repo only:

```
d=$PWD; until [ -e "$d/.git" ]; do [ -n "$d" ] || exit 0; d=${d%/*}; done; [ -d "$d/.claude/tdd" ] || exit 0; "<runtime>" -I -S "<managed>/hooks.py" stop --history-dir "…" --git "…" --test-python "…"
```

It fails open: if the runtime or py312 is broken, the stop is allowed, and CI covers the result. A session
started outside a tdd repo isn't gated locally; the edit guard and CI still apply.

### 10.4 SessionStart logger

Logs the session's start, repo, branch and whether maintenance mode is on.

### 10.5 Shell guard (phase 1b)

A PreToolUse hook on `Bash|PowerShell`.

It denies obvious writes to protected paths and `.git/**`:
- redirection with `>`;
- `sed -i`, `Set-Content`, `Out-File`, `mv`, `rm`;
- `git checkout --`, `git restore`.

It also denies:
- `--no-verify`;
- `-c core.hooksPath` and `git config core.hooksPath`;
- `git update-ref refs/remotes/…`.

It allows exactly one restore form, `git restore --source=origin/main -- <protected path>`.

It is heuristic and fails open. Phase 1b keeps only the rules the pilot's event log shows are
needed. A versioned `.githooks/` pre-commit integrity check may be added as a convenience.

### 10.6 Maintenance mode (phase 1b)

Maintenance mode lets an agent edit maintenance paths in a repo.

- **Switch:** `TDD_MAINTENANCE=1`, set in a terminal before you start `claude` there. Hooks read
  Claude Code's own environment, which an agent can't change.
- **Terminal only.** Never set it for VS Code, where every session in the window would inherit it.
  If Claude Code exposes its entry point to hooks, the hooks refuse maintenance mode outside the
  terminal CLI.
- If `TDD_MAINTENANCE` appears in any settings file's `env` block, the hooks ignore it and log a
  critical event.
- The SessionStart logger records every maintenance session.
- The change still goes through a maintenance PR that you merge.

Until phase 1b, you edit maintenance paths outside Claude.

## 11. CI

**`tdd-path-guard.yml`** runs on `pull_request_target`, so it always uses `main`'s workflow and
`main`'s `tools/tdd/`. It reads the PR's diff without executing any PR code. The profile,
`protected.txt` and locks it applies come from `main`, never from the PR's merge-base, so a branch
cut before the tdd pack existed is checked like any other; the merge-base only sets the diff. It:
- classifies the PR (§7.1) and fails on mixed PRs;
- fails if a spec PR changes the tests of another active change without re-locking it (that
  change's lock file must be in the same PR);
- fails if a spec PR comes from an `impl/*` branch (a legitimate spec PR never does), and notes
  every re-lock so changed locked tests stand out in review;
- fails if an existing lock changes outside a spec PR;
- fails if a signature in an active lock changes in an implementation PR;
- fails on stub-only violations in a spec PR;
- flags maintenance PRs for your attention.

**`tdd-spec.yml`** runs on `pull_request` and on pushes to `main`, using the controlled run
(§9.3) with the gate config from `main`:

| Tests | Requirement |
|---|---|
| Graduated | Pass |
| Active lock, on any PR or push | Pass or right-reason failure |
| Active lock `<id>`, on the PR from `impl/<id>` | Pass |

Before running the tests, it applies the local gate's lock checks against the locks on `main`:
- absence records;
- locked signatures;
- locked test files, except for a change this PR re-locks (its lock differs from `main`'s);
- the check that project code doesn't reach into the gate (§9.2).

Every lock the PR adds or changes is also checked against the PR's own files (tests, stubs,
absence records). A test edited after `tdd lock` therefore fails the spec PR instead of
surfacing after merge. On `spec/<id>` the Stop gate does the same and asks for `tdd lock <id>`
again.

So anything the local gate catches, CI catches too.

A red `main` means a graduated test broke, or an active-lock test is failing for the wrong reason.
Either is fixed as a bug through the normal cycle. Between part PRs, an active-lock test can
regress from passing to a right-reason failure. The final `impl/<id>` PR catches this.

**Vendored tools** are checked against their `MANIFEST.json` (version and hashes).

**Bootstrap:** the PR that first adds `tdd-path-guard.yml` isn't checked by it, so you review that
PR by hand.

**Permissions:** both workflows run with `contents: read` and `pull-requests: read`, and pass
`GH_TOKEN`, so CI can use GitHub's record of merged PRs to recognise retirement.

**Before merging**, the agent posts the `gh pr checks` output as a PR comment, because checks can't
be made required on the current plan.

faultline's `tdd-spec` job runs automatically on its committed snapshots. `pricer-ci.yml` stays
manual until its data question is settled.

## 12. Observability and budget

### 12.1 Event log

The hooks append these events to `ClaudeConfig/tdd-history/<project>/events.jsonl`:
- edit-guard denials (shell-guard denials from 1b);
- gate blocks and give-ways;
- budget exhaustion;
- disputes;
- `red_check` rejections;
- tampering;
- unrecognised retirements;
- session starts.

Each event records time, branch, change id, type, path or test, and session id. The edit guard
denies file-tool writes to the log. The log is committed to ClaudeConfig, so any other change to it
shows in `git diff`.

### 12.2 Iteration budget

The gate counts blocks per change in `.claude/tdd/state/<id>.json`, across sessions. The default
budget is 8, set in `profile.toml`. A per-iteration time or turn cap will follow when cost caps are
added, built on the existing per-call cost tracking.

## 13. Changes outside the tdd components

**Global `CLAUDE.md`, quality gate.** Replace the "Tests use fakes/stubs…" rule with:

> Tests use no network, no heavy models (torch) and no real services. Fakes are allowed only at I/O
> boundaries (network, LLM provider, S3, external DB), behind a `base.py` interface. The unit under
> test, its numerics and its data transforms are never mocked. Expected values come from an
> independent source.

**Global `CLAUDE.md`, other additions:**
- the policy paragraph (§3);
- in the Git section: "Never bypass git hooks (`--no-verify`, `core.hooksPath`)."

**Project files:**
- one pointer line in each project's root `CLAUDE.md`;
- `hypothesis` added to each project's requirements;
- `.claude/tdd/state/` added to each project's `.gitignore`.

Existing project hooks stay as they are.

**`ClaudeConfig/.gitignore`:** allow `tdd-history/`.

**Your actions:**
- disable squash and rebase merging in the three repos' GitHub settings;
- run the managed install script as administrator.

## 14. Rollout

| Phase | Work | Done when |
|---|---|---|
| 1a | Tools with their own tests: `spec_lint`, `red_check`, `lock`, `gate`, `path_guard`, `verify`, the outcome classifier. Managed install: edit guard, Stop gate, SessionStart logger. Event log. Workflow templates. Global `CLAUDE.md` changes. | All checks in §14.1 pass in a scratch repo |
| 2 | **faultline pilot**: `hypothesis`; pack, `profile.toml`, oracle catalogue; public-domain or synthetic snapshot; vendored tools; workflows (bootstrap PR reviewed by hand); one real change through the full cycle | One change through spec PR → implementation PR → retirement with no workarounds |
| 1b | Shell guard rules and maintenance mode, based on the pilot's event log | Each rule is backed by an observed event or a pilot need |
| 3 | **XbrlRag**: pack; golden set and numeric grader in spec form; case-id fake provider; live eval | One change through the cycle, including a live eval |
| 4 | **MacroRAG**: split `test/` into `test/spec/` and `test/unit/`; pack; retrieval fixture corpus | One change through the cycle |
| 5 | Review from the event log after about a month; decide on held-out cases (§15) and a cost cap | Decisions recorded |

### 14.1 Phase 1a checks

**Claude Code and the machine**
1. `disableAllHooks: true` in project or local settings does not disable managed hooks. If it
   does, managed settings also set `allowManagedHooksOnly`, and the projects' own hooks move into
   the managed install.
2. A `managed-settings.d/` file is read and its hooks run.
3. The exact `-p` name of each plugin with autoload off (e.g. `langsmith_plugin`).
4. `<spec_root>/conftest.py` at the `--confcutdir` boundary is loaded.
5. Timing, measured and recorded in §10.2: the Stop gate costs no more than a no-op hook outside
   tdd repos; the edit guard's per-edit cost is known and accepted.
6. A broken interpreter blocks edits to tdd repos and nothing else.
7. Managed hook commands run in Git Bash. If they don't, wrap them in `bash -c` with the absolute
   Git Bash path.
8. After install, `icacls` shows Users have no write access to `C:\Program Files\ClaudeCode\`, and
   a write there from a Claude session is denied.

**Edit guard**

9. Each of these edits to a protected file is denied:
   - from a session started in ClaudeConfig;
   - through a `..` path;
   - with mixed separators;
   - in a sibling `git worktree`, whether or not its folder name contains the repo name;
   - through a directory junction;
   - in `/d/…` form, if the file tools accept it;
   - through each file-writing tool.

**Gate**

10. A locked test can't be edited on an impl branch.
11. A test and lock changed together on a branch are caught by the gate and the path guard.
12. Each of these fails the gate:
    - a new root `conftest.py`;
    - `PYTEST_ADDOPTS`;
    - an added skip;
    - a plugin added through a branch `profile.toml`;
    - a repo-level `pytest.ini` or `[tool.pytest]` change;
    - a changed locked signature.
13. A branch cut before a lock merged reports no tampering.
14. After a re-lock, an un-rebased impl branch is blocked with "rebase".
15. With a spec PR merged on GitHub but not fetched, the gate fetches and finds the lock. Offline,
    it reports the ref's age.

**Red check, classifier, CI, retirement**

16. An `isinstance`-only test is rejected by `red_check`. A dataclass return type gets a
    zero-field variant.
17. The classifier separates `AssertionError`, the exact `NotImplementedError`, other exceptions,
    and hypothesis `ExceptionGroup` / `FlakyFailure` cases.
18. CI:
    - `main` stays green after a spec PR merges;
    - an unrelated PR isn't red;
    - the `impl/<id>` PR must turn its lock green.
19. Retirement is recognised from a real GitHub merge commit from `impl/<id>`, not from
    `impl/<id>--part1`. An implemented but unrecognised lock triggers the warning.

## 15. Known limits

- **Merging depends on you reading the checks.** The agent's checks comment puts them in front of
  you.
- **Packages installed into the local conda env** can affect the local pytest run. CI's fresh
  environment catches this.
- **Copies of a repo that aren't git working trees** aren't guarded locally. CI covers them.
- **Git Bash, `git.exe` and the project interpreter are user-writable on this machine** (§10.1),
  so a determined agent could still disable the local hooks. CI and your merge are unaffected.
- **The tdd lens is advisory.** Property tests, wrong-value red checks and fresh seeds at verify
  are what defend against code fitted to the tests.
- **Project code runs in the test process.** Code in `src/` that tampers with pytest or the
  gate's report at run time can't be prevented, locally or in CI. The gate-reference check (§9.2)
  catches the direct routes: naming the gate, or importing test machinery, including via
  `__import__` or `importlib`. A disguised route, such as building the names from strings, is
  left to your review of the implementation diff.
- **Held-out test cases** are not part of this design. If phase 5 adopts them, you write them
  outside Claude, because an agent-written case would be readable in its session transcript.
- **A broken py312 blocks edits to the tdd repos** until it is fixed.
- **Gate snapshots are frozen by design.** Data freshness is checked by the existing real-data
  tests, outside the gate.
- **Committed market data must be licensed** for redistribution (§8.4).

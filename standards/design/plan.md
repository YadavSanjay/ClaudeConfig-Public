# Plan: Python coding standards for faultline, XbrlRag and MacroRAG

Status: **draft for approval** (2026-10-05). Not implemented.

Scope: **faultline** (pricing and risk), **XbrlRag** and **MacroRAG** (financial RAG).
RagInference is out of scope.

## 1. Purpose

Give every Claude Code agent (main session, general-purpose and custom subagents) one Python coding
standard, plus domain rules for pricing/risk and for RAG.

- **The standard** is the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
  with a short list of house deviations. It applies to new code and to code a change touches.
- **Enforcement:** tooling enforces whatever it can check, and prose is kept for judgement only.

## 2. Starting point (2026-10-05)

| Repo | Ruff | Formatter | mypy | On-edit hook | Stop hook | CI lint/type | Branch |
|---|---|---|---|---|---|---|---|
| faultline | broad set (`E,W,F,I,UP,B,C4,SIM,RET,PIE,PERF,RUF`), line length 120, py312, clean | none | strict on `src/{common,pricer,scenariogen,marketdata}`, clean | none | none | `pricer-ci.yml`, **manual only** (pytest needs gitignored market data) | `main` |
| XbrlRag | none | black + isort, defaults (line length 88) | none | isort + black | `pytest test/unit` when `.py` changed; no `stop_hook_active` check | none (pytest only) | `feat/review-lens-pack` (unmerged) |
| MacroRAG | `F,E9`, line length 100, py312 | none | `mypy.ini`, clean, not strict | `ruff check --fix` | pytest subset on every stop | ruff + mypy + pytest, blocking | `feat/review-lens-pack` (unmerged) |

Also relevant:

- **Shared tools.** All three repos use the local `py312` conda env, with ruff 0.14.2.
- **Unpinned versions.** MacroRAG CI uses `ruff-action@v3` (latest ruff) and installs packages
  without versions. All workflows reference GitHub Actions by major tag.
- **Lenses.** Each repo already has review-core lenses that state its domain rules in detail.
- **tdd-spec.** Phases 2–4 (faultline, XbrlRag, MacroRAG) will install a few things in each repo:
  - a **managed** Stop gate: admin-installed, on a stdlib-only interpreter, so it has no ruff or
    mypy;
  - a `<spec_root>`: faultline `tests/spec`; XbrlRag and MacroRAG `test/spec`;
  - **maintenance paths**: files directly in `<spec_root>/`, `.github/workflows/**` and
    `.claude/settings.json`.

### 2.1 Baselines (ruff 0.14.2, `--target-version py312`)

| Repo | Target tier | Findings | Auto-fixable (`UP,I,W`) | Burn-down entries after the fix pass (file × rule, `src`) |
|---|---|---|---|---|
| faultline | tier 1 + `NPY,PD` | clean at its current config | — | 40 |
| XbrlRag | tier 0, line length 100 | 2,700 + 489 `E501` | 2,305 | ~640 + `E501` entries |
| MacroRAG | tier 0 | 620 | 540 | ~170 (falls once the FastAPI `B008` setting applies) |

The entry counts include the Google-profile rules in the tiers (§5.4) and exclude `N813`, which is
ignored by design.

**Distance from the Google guide** (`src`):

| Area | faultline | XbrlRag | MacroRAG |
|---|---|---|---|
| Docstring findings (`D`, Google convention) | 861 | 871 | 1,025 |
| Naming (`N`) | 16, all `import QuantLib as ql` | 21, mostly Arelle plugin API names | 6, exception names without `Error` |
| f-strings in logging calls (`G004`) | 0 | 175 | 0 |
| Lines importing a class or function directly (`from x import Class`) | 1,416 | 762 | 988 |

**Hook costs on this machine:**

| Step | Time |
|---|---|
| ruff on 5 files | 0.27 s |
| `conda run` startup | 3.2 s |
| mypy warm, faultline | 2.8 s (cold: 23 s) |
| mypy warm, MacroRAG | 11 s |

## 3. Decisions

1. **Standard: the Google Python Style Guide, with house deviations.** The guide is named, not
   reproduced; agents already know it. These deviations override it:

   | Guide | House rule | Why |
   |---|---|---|
   | Import modules only, not classes or functions (§2.2) | `from x import Class` is allowed | ~3,200 existing lines do this; mixing both styles in one file is worse than either |
   | One import per line (§3.13) | ruff `I` decides grouping | forcing single-line imports would rewrite every file |
   | 80-character lines (§3.2) | the repo's line length (decision 3) | existing code |
   | Use a formatter (§3) | ruff lint only, no formatter (decision 3) | existing convention in faultline and MacroRAG |
   | Avoid abbreviations in names (§3.16) | standard import aliases are allowed (`np`, `pd`, `ql` for QuantLib) | universal convention |
   | Naming (§3.16) | names dictated by an external API keep that API's form (Arelle plugin hooks such as `modelXbrl`) | can't be renamed |

   The guide's own allowances are used as written, in particular:
   - catch-all `except` at an isolation point (§2.4), which covers faultline's per-trade fault
     isolation;
   - short names that follow established mathematical notation in numeric code, with a reference
     (§3.16.4).

2. **Where the guide applies.**

   | Code | Rule |
   |---|---|
   | New file, function or class | follows the guide in full |
   | Refactored function or class | the **whole unit** is brought to the guide: signature types, docstring, names, exception handling |
   | Small edit inside a function | the changed lines follow the guide; the rest of the unit stays as it is |
   | Untouched code, including neighbours in the same file | not restyled in a feature change; otherwise "match the surrounding code" holds |
   | Bulk restyling | only through the safe-fix pass and the burn-down list (decision 4), never mixed into feature work |
3. **Formatting: ruff lint only, no formatter, in all three repos.**
   - No black, isort or `ruff format`. Imports are sorted by ruff `I`; line length is checked by
     `E501`.
   - Line length: faultline 120, MacroRAG 100, XbrlRag **100**.
   - No repo-wide reformat.
4. **Ratchet.** Every repo reaches a tier the same way:
   1. Set `target-version = "py312"` and the tier's rules.
   2. Run one safe-fix pass, excluding `<spec_root>`:
      `ruff check --fix --fixable UP,I,W --extend-exclude <spec_root> .`
      It is a whole-repo diff, so it runs only when the repo has no open branches.
   3. Generate the burn-down list from what is left (decision 8).
   4. Gate on the result (decision 10).

   A repo moves up a tier when its current burn-down list is empty and the next tier leaves at
   most **50** entries after a safe-fix pass.
5. **Package each rule by how it can be checked** (§4.1).
   - Tool-checkable rules → tool config, never prose.
   - Judgement rules → one short `.claude/rules/` file per repo.
   - Root `CLAUDE.md` → one pointer line.
   - No skill carries the standards: skills load only when invoked and are not given to subagents
     automatically.
6. **One owner per rule.**
   - Testing rules: tdd-spec §8.
   - Architecture conventions (fail-loud, typed config, pydantic at boundaries): the global
     `CLAUDE.md`.
   - Review detail: the lenses. Each domain rule file is a short digest of its repo's lenses.
7. **Stop gate lives in each repo's own project hook**, never in the tdd-spec managed gate.
   - faultline gets a new small Stop hook; XbrlRag and MacroRAG extend theirs.
   - Once tdd-spec lands, each repo has two independent Stop gates.
   - **What runs:**
     - ruff on the `.py` files changed against `git merge-base HEAD origin/main`, plus untracked
       files, with protected paths (`<spec_root>`, tdd-spec maintenance and protected paths,
       XbrlRag's guard list) left out;
     - the new-code check (decision 13) on the same files;
     - the ratchet check, when a config file changed;
     - mypy, on the full configured targets, only where the repo is clean and the run fits the
       time budget (faultline yes; MacroRAG CI only, until its 11 s run is fixed).
   - **Blocking:** at most once per turn. When `stop_hook_active` is set, the hook allows the stop
     and reports the findings.
   - **Fail open, with a warning,** if `origin/main`, the merge-base, the tools or the time budget
     fail.
8. **The burn-down list may only shrink, and config may only tighten.**
   - **Its own file.** The list lives in `ruff-burndown.toml` (`[lint.per-file-ignores]`). The main
     config pulls it in with `extend`; permanent test entries go in `lint.extend-per-file-ignores`.
   - **Ratchet check.** `check_standards.py ratchet` (one tested script per repo, §8.2) fails,
     against the merge-base, if any of these happen:
     - the burn-down file gains an entry;
     - the ruff config loses a rule or gains an ignore;
     - the mypy config gains an ignore or override;
     - a pin is loosened.
   - **Deliberate loosening** needs a `Lint-Ratchet: <reason>` commit trailer, which shows at
     merge. The final guarantee is the human merge.
9. **Everything is pinned.**
   - **Dev tools and CI packages:** exact `==` versions in `requirements-dev.txt`, which CI installs
     from. ruff and mypy have one version across the three repos.
   - **GitHub Actions:** pinned to a commit SHA, with the version in a comment. `ruff-action` gets
     `version:`.
   - **Upgrades:** a deliberate ratchet step, done in all three repos together.
   - **Runtime dependencies:** pinned in phase D.
10. **New gates are advisory at first.**
    - **CI:** the new lint job runs with `continue-on-error: true` and writes findings to the job
      summary.
    - **Stop:** the new steps report and never block.
    - **Existing blocking gates stay:** MacroRAG CI ruff (kept on `--select F,E9`) and mypy, and
      each repo's Stop pytest step.
    - **Turning hard** is a per-repo decision by the owner, after all of these hold: at least two
      weeks advisory; no open false positives; the Stop step within budget; the burn-down list not
      growing.
11. **Hooks run only when needed and stay fast.**
    - **On-edit:** matcher `Edit|Write|MultiEdit`, `.py` files only, ruff on that one file. Budget:
      under 1 s.
    - **Stop:** stops at the first "nothing to do":
      1. no `.py` or config file changed → exit (under 0.5 s);
      2. nothing changed since the last check → exit. A fingerprint of the changed files' contents
         is kept in the gitignored `.claude/state/standards-stop.json`;
      3. ruff on the changed files;
      4. the new-code check on the changed lines;
      5. the ratchet check, if config changed;
      6. mypy, where allowed.

      Budget: about 5 s, warm.
    - **Calls:** tools are called directly from the env, never through `conda run`. No network.
    - **Timeouts** are set per hook in `.claude/settings.json` (on-edit 10 s, Stop 60 s), and the
      hook fails open when one is reached.
    - **Existing pytest steps** get the same skip checks.
12. **Spec tests are linted by a nested config owned by tdd-spec.**
    - `<spec_root>/ruff.toml` extends the repo config with `lint.select = ["F", "E9"]`, so locked
      tests never pick up new rules.
    - It ships in the tdd-spec project pack (tdd-spec §8.8), because files directly in
      `<spec_root>/` are tdd-spec maintenance paths.
13. **New and changed code is checked against the guide on changed lines only.**
    - **Why:** some guide rules have far too many existing findings to burn down; docstrings alone
      have 861 / 871 / 1,025. A burn-down list for them would exempt nearly every file, so these
      rules are checked by diff instead.
    - **How:** `check_standards.py new-code` (§8.2) runs ruff with the new-code profile (§5.4) on
      the changed files, and keeps only findings on lines changed against the merge-base. A new
      public function without a Google docstring is flagged; existing ones are not.
    - **Where:** in the same Stop hook and CI job as the other checks, under the same skip rules
      (decision 11). Advisory at first (decision 10). No new hook.
    - **Not covered:** the "whole unit on refactor" rule (decision 2) is judgement. Tools see changed
      lines, not intent. It is stated in `python.md` and checked at review.
14. **Not adopted.**
    - `BLE001`: broad `except` at an isolation point is allowed by the guide (§2.4), and faultline
      relies on it for per-trade fault isolation.
    - `noqa` comments for the burn-down: the burn-down file is used instead.
    - Git pre-commit hooks: the Stop gate and CI already cover them.
    - `paths:` scoping on domain rules: each repo has a single domain, and scoping would miss new
      packages.
    - A shared cross-repo config or sync tool: it would tie the repos to this machine.

## 4. Design

| Layer | Owns | Lives in |
|---|---|---|
| **Coding rules** | Judgement part of style, types, errors, domain conventions | Global `rules/python.md` (`paths: ["**/*.py"]`); each repo's `.claude/rules/quant.md` or `rag.md` (unscoped) |
| **tdd-spec** | How code is tested; spec-test lint config | `skills/tdd-spec`, each repo's `.claude/tdd/` |
| **review-core lenses** | How code is checked at review | Each repo's `.claude/review/` |
| **Tooling** | What is enforced | Pinned ruff and mypy, repo config, `ruff-burndown.toml`, `check_standards.py` (ratchet and new-code); run by project hooks and CI |

How each mechanism reaches agents:

- **Root `CLAUDE.md` and `.claude/rules/`** load for the main session and for general-purpose and
  custom subagents. Built-in Explore and Plan skip them; both are read-only.
- **Skills** are not loaded into subagents automatically.
- **Hooks** fire for subagents' tool calls too.

### 4.1 When and how the standards are applied

| Kind of rule | Examples | Packaged as | Applied |
|---|---|---|---|
| Machine-checkable, whole file | `E722`, `DTZ`, `S101`, `N`, `G004`, `RUF013`, `RUF100`, `PGH003`, `NPY002`, `I`, type errors, a weakened config or loosened pin | ruff/mypy config, burn-down file, ratchet check | on edit (auto-fix), at Stop and in CI |
| Machine-checkable, changed lines only | Google docstrings (`D`) | new-code check (decision 13) | at Stop and in CI |
| Guide, judgement | the Google guide and its deviations, "whole unit on refactor", function size, power features | `rules/python.md` | loaded when `.py` files are involved |
| Domain, judgement | explicit day count, units in names, provenance, untrusted retrieved text | `.claude/rules/{quant,rag}.md` | always loaded in its repo |
| Judgement, at review | all of the above, plus repo-specific failure modes | review-core lenses; each repo's review profile points to `python.md` | at review |
| Procedure | spec tests, reviews | skill | when invoked |
| Pointer | where the standards live, how to run the gate | one line in the root `CLAUDE.md` | always loaded |

Timeline:

1. **Writing:** the rule files steer the agent.
2. **After each `.py` edit:** ruff auto-fix on that file; never blocks.
3. **Agent stop:** only if relevant files changed: ruff, the new-code check, the ratchet check,
   mypy where allowed.
4. **PR (CI):** pinned tools, the burn-down list, the new-code check, the ratchet check.
5. **Review:** lenses, reported P0–P3.

## 5. Content

### 5.1 `rules/python.md` (global, `paths: ["**/*.py"]`, about 35 lines)

- **Follow the Google Python Style Guide** for new code and for code you touch, with these house
  deviations: the table from decision 1, copied in, plus the guide allowances it names.
- **Scope:** new files, functions and classes follow the guide in full. When you refactor a
  function or class, bring the whole unit to the guide (types, docstring, names, exceptions). For a
  small edit, only the changed lines. Don't restyle untouched code, including neighbours in the same
  file.
- Never weaken lint or type config to pass a gate: no new ignores, burn-down entries, mypy
  overrides, removed rules or loosened pins. Fix the code, or report.
- Type hints on new and changed functions.
- Docstrings in Google format (`Args:`, `Returns:`, `Raises:`) on new public functions, classes and
  modules. Don't repeat the type hints in prose.
- A `# noqa` or `# type: ignore` names the code and says why.
- Randomness takes a seed or a `Generator` as a parameter; no module-level random state.
- New dependencies and tools are added with an exact pinned version.
- Testing: tdd-spec §8. Architecture conventions: the global `CLAUDE.md`.

### 5.2 faultline `.claude/rules/quant.md` (about 10 lines, digest of the `numerics.md` lens)

- Day count, calendar, business-day convention, settlement days and compounding are explicit
  parameters or come from the trade or config, never from hidden defaults.
- Units and currency are in names or types.
- Vectorise hot numeric paths; object-per-instrument QuantLib code is fine.
- Validate inputs at boundaries, and raise if a `NaN` reaches an output.

### 5.3 XbrlRag and MacroRAG `.claude/rules/rag.md` (about 10 lines each, digest of each repo's lenses)

Shared:

- Timeouts and bounded retries on every LLM and vector-DB call.
- Prompts are versioned files, not inline strings.
- Every answer carries source and chunk provenance.
- Chunking is deterministic.
- Retrieved text is untrusted input (prompt injection).
- No secrets or PII in logs.

XbrlRag: XBRL fact values have scale and sign applied explicitly, in one place.
MacroRAG: units, scale and sign of macro series are explicit.

### 5.4 Ruff tiers

| Tier | Rules |
|---|---|
| 0 | `E,F,W,I,UP,B,RUF100,PGH003` + Google profile `N,G004,RUF013` |
| 1 | tier 0 + `SIM,C4,RUF,DTZ,S,PT,T20,TD` |
| faultline extra | `NPY,PD` |
| New-code profile (decision 13, changed lines only) | `D` with `lint.pydocstyle.convention = "google"` |

Every repo config also has:

- `target-version = "py312"` and its line length (decision 3);
- `extend = "ruff-burndown.toml"`;
- permanent test entries: `tests/**`, `test/**` = `S101`, `PT018`, `T20`, `D`;
- `ignore = ["N813"]`, so standard aliases such as `import QuantLib as ql` are allowed
  (decision 1).

XbrlRag also has a permanent `N802,N803` entry for `src/ingest/sec_edgar/ingest/arelle_plugins/**`,
where Arelle's plugin API dictates the names (decision 1). Its other naming findings go on the
burn-down list, as do MacroRAG's exception names without `Error`.

RAG repos also have `lint.flake8-bugbear.extend-immutable-calls` for the FastAPI helpers
(`fastapi.Depends`, `Query`, `Header`, `Body`, …).

mypy configs gain `warn_unused_ignores = true` and `enable_error_code = ["ignore-without-code"]`.

## 6. Phases

### Phase 0: remaining checks

- Confirm `.claude/rules/` loads, including from a subagent (`/context`).
- Confirm user-level rules load from `CLAUDE_CONFIG_DIR/rules/` with `paths:`. If not, `python.md`
  becomes a short section of the global `CLAUDE.md`.
- Find why MacroRAG's warm mypy takes 11 s.
- After phase B, time each repo's Stop step. Any step over budget moves to CI only.

### Phase A: ClaudeConfig

- Branch `feat/coding-standards`.
- Add `rules/python.md`.
- One pointer line in the global `CLAUDE.md`.
- Allow `rules/` and `standards/` in `.gitignore`.
- Propose the `<spec_root>/ruff.toml` addition to the tdd-spec project pack.

### Phase B: one repo at a time

Steps in each repo:

1. Pin everything: `requirements-dev.txt`, CI installs from it, Actions by SHA.
2. ruff config: `target-version`, line length, tier rules, test entries, `extend`.
3. Safe-fix pass, excluding `<spec_root>`, in its own commit, with no open branches.
4. Generate `ruff-burndown.toml`.
5. Add `tools/standards/check_standards.py` and its tests (§8.2).
6. CI lint job on every push and PR, advisory: ruff, the new-code check, the ratchet check, and
   mypy where configured.
7. Hooks:
   - on-edit and Stop hooks as in decisions 7 and 11, advisory;
   - timeouts in `.claude/settings.json`;
   - **track the hooks in git:** today no repo tracks `.claude/settings.json`, and MacroRAG doesn't
     track its hook scripts either, so the hooks only exist on this machine. Personal permissions
     move to `.claude/settings.local.json`, and `.gitignore` is updated (§8.2).
8. Domain rule file.
9. Pointer line in the root `CLAUDE.md`. One line in `.claude/review/profile.md`: touched code is
   reviewed against the Google guide and its deviations in `python.md`.

Per repo:

1. **faultline** (now, before tdd-spec phase 2):
   - tier 1 + `NPY,PD` (40 burn-down entries);
   - split `pricer-ci.yml` so lint + mypy + ratchet run on every push and PR without market data
     (pytest stays manual);
   - new on-edit and Stop hooks (ruff, ratchet, mypy).
2. **XbrlRag** (after `feat/review-lens-pack` merges, before tdd-spec phase 3):
   - tier 0, line length 100;
   - the on-edit hook drops isort + black for `ruff check --fix`;
   - black and isort are removed from dev tooling;
   - the black/isort instructions in `CLAUDE.md` are replaced;
   - a CI lint job is added;
   - `stop_gate.py` gains the skip fingerprint, ruff, the new-code and ratchet checks, and
     `stop_hook_active` (no mypy).
3. **MacroRAG** (after `feat/review-lens-pack` merges, before tdd-spec phase 4):
   - tier 0;
   - CI: pin `ruff-action` and packages; keep the blocking ruff step on `F,E9`; add the advisory
     tier-0 job and the ratchet check;
   - `test-gate.ps1` gains the skip checks (now also gating its pytest run), ruff, and the new-code
     and ratchet checks; mypy only once it fits the budget.

### Phase C: later

Bring the XbrlRag and MacroRAG root `CLAUDE.md` files towards ~200 lines, moving path-specific parts
into `.claude/rules/`, with the owner's approval.

### Phase D: later, one PR per repo

Pin runtime dependencies (`requirements*.txt` that feed containers). This changes deployed images,
so it needs its own test run.

## 7. Ordering and parallel safety

- **tdd-spec order.** Phase B runs in each repo **before** that repo's tdd-spec phase, because CI
  workflows and `.claude/settings.json` then become tdd-spec maintenance paths. If it runs later,
  those edits go through tdd-spec maintenance mode.
- **Safe-fix passes** run only with no open branches, and they never touch `<spec_root>`.
- **Untouched by this plan:** tdd-spec and review-core files, and the managed Stop gate.

## 8. Implementation file map

Every file this plan creates or changes, with when it is loaded and who uses it.

**Action:** **New** = created; **Edit** = existing file changed; **Gen** = generated by a tool and
committed; **Runtime** = written at run time, never committed.

**How Claude Code loads things:**
- **Session start:** the user-level `CLAUDE.md`, the root `CLAUDE.md`, and every `.claude/rules/*.md`
  without `paths:`. These go to the main session and to general-purpose and custom subagents.
- **On file access:** a rule file **with** `paths:` loads when the agent reads or edits a matching
  file.
- **Hooks** are read from `.claude/settings.json` at session start, and each fires on its event.
  This includes subagents' tool calls.

### 8.1 ClaudeConfig (phase A)

| Path | Action | Contents | Loaded / used |
|---|---|---|---|
| `rules/python.md` | New | Google guide + deviations table, scope rules, judgement rules (§5.1). About 35 lines; frontmatter `paths: ["**/*.py"]` | User-level rule, in every project. Loads when the agent reads or edits a `.py` file, for the main session and general-purpose/custom subagents. Not in non-Python projects. Phase 0 confirms the `CLAUDE_CONFIG_DIR/rules/` location; the fallback is a short section in `CLAUDE.md`. |
| `CLAUDE.md` | Edit | One line under Code & docs style: "Python: Google style guide with house deviations, see `rules/python.md`" | Session start, every project. |
| `.gitignore` | Edit | `!rules/`, `!standards/` | git only. |
| `standards/design/plan.md` | exists | This plan | People only; never loaded by agents. |

Proposed to tdd-spec, made there and not by this plan: the tdd-spec project pack gains a
`<spec_root>/ruff.toml` template (decision 12). It is created in a repo when that repo's tdd-spec
phase installs the pack, and ruff then uses it for every file under `<spec_root>`.

### 8.2 Every repo (phase B)

These files exist in all three repos with the same purpose. The repo-specific names and states are
in §8.3–8.5.

| File | Purpose | Loaded / used |
|---|---|---|
| `.claude/rules/quant.md` or `rag.md` | Domain digest of the repo's lenses (§5.2, §5.3) | Session start in that repo (no `paths:`), main session and subagents. |
| Root `CLAUDE.md` | One pointer line: where the standards live, how to run the checks | Session start in that repo. |
| `.claude/review/profile.md` | One line: review touched code against the guide and `python.md` | When `/review-core` runs in the repo. |
| ruff config | Tier rules, line length, `target-version`, `extend = "ruff-burndown.toml"`, permanent ignores (§5.4) | Every ruff run: on-edit hook, Stop hook, CI, manual. |
| `ruff-burndown.toml` | Burn-down list (`[lint.per-file-ignores]`) | Pulled into every ruff run via `extend`. The ratchet check compares it with the merge-base. Created by `check_standards.py burndown`, then only shrinks. |
| mypy config | `warn_unused_ignores`, `enable_error_code = ["ignore-without-code"]` | Every mypy run: Stop hook (where allowed) and CI. |
| `requirements-dev.txt` | Exact pins: ruff 0.14.2, mypy 1.18.2, pytest 9.0.1 (faultline's current pins), plus every package CI installs | CI install step; setting up the `py312` env. The ratchet check reads it for loosened pins. |
| `tools/standards/check_standards.py` | One script, stdlib + ruff/mypy subprocesses, about 250 lines. Subcommands below | Called by the Stop hook (`stop`) and CI (`ratchet`, `new-code`); `burndown` is run by hand. |
| Tests for the script | Unit tests for each subcommand, using temporary git repos and no network | The repo's normal pytest run and CI. |
| CI workflow(s) | Advisory `lint` job on push and PR. All `uses:` pinned by SHA. `fetch-depth: 0` so the merge-base exists | GitHub Actions on every push and PR. |
| `.claude/settings.json` | Hook registration: PostToolUse matcher `Edit\|Write\|MultiEdit`, Stop hook, timeouts (10 s / 60 s). **Now tracked in git**, with no personal permissions | Read by Claude Code at session start. |
| `.claude/settings.local.json` | Gets the personal `permissions` moved out of `settings.json` | Session start; local only (already gitignored). |
| On-edit hook script | `ruff check --fix` on the edited `.py` file only | PostToolUse, after each `Edit`/`Write`/`MultiEdit`; exits at once for non-`.py` files. |
| Stop hook script | Thin wrapper: runs `check_standards.py stop`, prints its report, then the repo's existing pytest step (skipped when the fingerprint says nothing changed) | Stop event, at the end of every turn. Usually exits in under 0.5 s. |
| `.claude/state/standards-stop.json` | Runtime fingerprint of the changed files at the last completed check | Written and read by `check_standards.py stop`. Never committed. |
| `.gitignore` | Whitelist `!.claude/rules/`, `!.claude/hooks/`, `!.claude/settings.json`; ignore `.claude/state/` | git only. |

**`check_standards.py` subcommands**

| Subcommand | What it does | Used by |
|---|---|---|
| `stop` | Runs the Stop sequence from decision 11: skip checks and fingerprint → ruff on the changed files → `new-code` → `ratchet` if config changed → mypy where allowed. Prints a report and exits 0 while advisory | Stop hook |
| `ratchet` | Decision 8: the burn-down list only shrinks; ruff/mypy config and pins only tighten; honours the `Lint-Ratchet:` trailer | Stop (via `stop`) and CI |
| `new-code` | Decision 13: Google-docstring findings on changed lines only | Stop (via `stop`) and CI |
| `burndown` | Writes `ruff-burndown.toml` from the current findings; refuses to add entries unless run with `--initial` | By hand, in phase B step 4 and at tier promotions |

The script is the same file in all three repos. The few repo differences (mypy on or off, test
paths, protected paths) come from a `[tool.standards]` table in `pyproject.toml`, or from
`standards.toml` where the repo has no `pyproject.toml`.

### 8.3 faultline

| Path | Action | Notes |
|---|---|---|
| `.claude/rules/quant.md` | New | §5.2 |
| `CLAUDE.md` | Edit | Pointer line |
| `.claude/review/profile.md` | Edit | One line |
| `pyproject.toml` | Edit | `[tool.ruff]`: tier 1 + `NPY,PD`, `N813` ignore, `extend`, test entries. `[tool.mypy]`: two settings. New `[tool.standards]`: mypy at Stop on; protected paths |
| `ruff-burndown.toml` | Gen | 40 entries |
| `requirements-dev.txt` | Edit | Already pins ruff, mypy, pytest; check that every line is exact |
| `tools/standards/check_standards.py` | New | |
| `tests/tools/test_check_standards.py` | New | |
| `.github/workflows/pricer-ci.yml` | Edit | New automatic `lint` job (ruff, new-code, ratchet, mypy) with no market data; the pytest job stays manual; Actions pinned by SHA |
| `.claude/settings.json` | Edit | Move permissions to `settings.local.json`; add hooks; start tracking |
| `.claude/hooks/standards_edit.py` | New | On-edit hook (stdlib launcher; calls the `py312` ruff directly) |
| `.claude/hooks/standards_stop.py` | New | Stop hook wrapper (faultline has no Stop pytest step today; none is added) |
| `.claude/hooks/_hooklib.py` | New | Shared helpers for the two hooks: event parsing, py312 lookup (`FAULTLINE_HOOK_PYTHON` overrides) |
| `.gitignore` | Edit | Whitelist and state entries |

### 8.4 XbrlRag

| Path | Action | Notes |
|---|---|---|
| `.claude/rules/rag.md` | New | §5.3 + XBRL scale and sign |
| `CLAUDE.md` | Edit | Pointer line; replace the black/isort instructions (lines 87–90) with ruff |
| `.claude/review/profile.md` | Edit | One line |
| `ruff.toml` | New | No `pyproject.toml` exists. Tier 0, line length 100, `N813` ignore, permanent `N802,N803` for `src/ingest/sec_edgar/ingest/arelle_plugins/**`, FastAPI immutable calls, `extend` |
| `standards.toml` | New | Repo settings for the checker: mypy at Stop off; protected paths (from `guard_protected_paths.py`) |
| `ruff-burndown.toml` | Gen | ~640 entries + `E501` |
| `requirements-dev.txt` | New | ruff, pytest and the CI packages, pinned; no black or isort |
| `tools/standards/check_standards.py` | New | |
| `test/unit/tools/test_check_standards.py` | New | Under `test/unit`, so its Stop gate and CI run it |
| `.github/workflows/ci.yml` | Edit | Add the advisory `lint` job; pin Actions by SHA |
| `.github/workflows/docker.yml`, `xbrl-regression.yml` | Edit | Pin Actions by SHA only |
| `.claude/settings.json` | Edit | Matcher `Write\|Edit` → `Edit\|Write\|MultiEdit`; timeouts; start tracking |
| `.claude/hooks/format_python.py` | Edit | isort + black → `ruff check --fix` |
| `.claude/hooks/stop_gate.py` | Edit | Call `check_standards.py stop` first; honour `stop_hook_active`; the fingerprint skip also gates its pytest run |
| `.claude/hooks/_hooklib.py` | Edit | Small helper to run the checker, if needed |
| `.claude/hooks/README.md` | Edit | Describe the new behaviour |
| `.gitignore` | Edit | Add `!.claude/rules/`, `!.claude/settings.json` and the state entry (`hooks/` is already whitelisted) |

### 8.5 MacroRAG

| Path | Action | Notes |
|---|---|---|
| `.claude/rules/rag.md` | New | §5.3 + macro units, scale and sign |
| `CLAUDE.md` | Edit | Pointer line |
| `.claude/review/profile.md` | Edit | One line |
| `ruff.toml` | Edit | `F,E9` → tier 0, `N813` ignore, FastAPI immutable calls, `extend` |
| `mypy.ini` | Edit | Two settings |
| `standards.toml` | New | Repo settings for the checker: mypy at Stop off until it fits the budget; protected paths |
| `ruff-burndown.toml` | Gen | ~170 entries |
| `requirements-dev.txt` | New | ruff, mypy, pytest and every package `ci.yml` installs, pinned |
| `tools/standards/check_standards.py` | New | |
| `test/test_check_standards.py` | New | |
| `.github/workflows/ci.yml` | Edit | Pin `ruff-action` and Actions by SHA; install from `requirements-dev.txt`; keep the blocking ruff step on `--select F,E9`; add the advisory `lint` job |
| `.claude/settings.json` | Edit | Timeouts; start tracking |
| `.claude/hooks/format-python.ps1` | Unchanged | Already runs `ruff check --fix` on the edited file; now tracked |
| `.claude/hooks/test-gate.ps1` | Edit | Call `check_standards.py stop` first; its fingerprint skip also gates the existing pytest subset |
| `.claude/hooks/guard-env.ps1` | Unchanged | Now tracked |
| `.gitignore` | Edit | Add `!.claude/rules/`, `!.claude/hooks/`, `!.claude/settings.json` and the state entry |

### 8.6 What is never created

- No skill for the standards.
- No new hook **events**; every hook is on PostToolUse or Stop. faultline gets its first two hooks.
- No shared config across repos, no sync tool, no pre-commit hook.
- No edits to tdd-spec, review-core or the managed Stop gate.

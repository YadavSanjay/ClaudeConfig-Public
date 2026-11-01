# Global conventions (user-level)

Cross-project house rules for my Claude Code work, loaded for **every** project via
`CLAUDE_CONFIG_DIR`. Each project's own root `CLAUDE.md` (and module-level ones) adds
project specifics and **overrides** anything here. If a project rule conflicts with a
rule below, the project rule wins.

These are distilled from my AI/Python projects (XbrlRag, MacroRAG, faultline,
RagInference). They are conventions, not architecture — the specifics
live in each repo.

## Environment (Windows dev machine)

- **Python: always the `py312` conda env.** Invoke as `conda run -n py312 <cmd>` (add
  `--no-capture-output` when you need live output). Never use the bare `python` on PATH —
  it lacks project dependencies.
- **`conda` is on the PowerShell PATH, NOT the Bash PATH.** Both tools exist; they are
  **not interchangeable**. Choose the tool by what the command starts with:

  | Run in **PowerShell** | Run in **Bash** |
  |---|---|
  | `conda`, `python`, `pip`, `pytest`, `ruff`, `mypy`, `uv`/`uvx` | `git`, `grep`, `find`, `sed`, `awk`, POSIX glue |

  **Before every Bash call, check the first word against that left column. If it matches,
  use the PowerShell tool instead.** This is the most-repeated mistake in this
  environment — it has recurred across many sessions *despite already being written
  here*, so treat it as a pre-flight check, not background knowledge. Failure signature:
  `bash: conda: command not found` — or worse, a bare `python` that resolves silently
  without the project's dependencies. If you see it, don't debug it: switch tools and
  re-run. A project may enforce this with a PreToolUse hook; otherwise it relies on you.
- **Node (UI work):** use the pinned toolchain, and prepend it to PATH when running `npm` if
  it isn't there. A project may pin its own.
- **Machine-specific paths** live in a local rule under `rules/`, not here.
- **Module runs usually need `PYTHONPATH`** set to `src` or the repo root — the exact
  form is per-project, see the project `CLAUDE.md`. Tests set their own path via conftest.

## Quality gate — run before you commit

- **`ruff` + `mypy` + `pytest`, all green.** Auto-fix with `ruff check --fix`. Several
  projects wrap this as a single target (e.g. `task ci`); use it when present.
- **Tests use no network, no heavy models (torch) and no real services.** Fakes are allowed
  only at I/O boundaries (network, LLM provider, S3, external DB), behind a `base.py`
  interface. The unit under test, its numerics and its data transforms are never mocked.
  Expected values come from an independent source. Adding an external dependency means:
  define/extend a `base.py` interface and provide a fake for tests.
- **Test-first in faultline, XbrlRag and MacroRAG** (any repo with `.claude/tdd/`): every new
  change starts with spec tests written from `docs/specs/<id>.md`, proven red, locked and merged
  before implementation — use the `tdd-spec` skill. Your branch sets your role (`spec/<id>`
  writes tests and stubs; `impl/<id>` writes `src/`). **Never change locked spec tests, locks,
  locked signatures or maintenance paths while implementing;** if a test looks wrong, write a
  dispute (`.claude/tdd/disputes/<id>.md`) and stop. Managed hooks enforce this; CI and the
  human's merge are the guarantee.
- **Don't fight the auto-format hooks.** `.claude/hooks/` may run `ruff check --fix` (or, in
  repos not yet ruff-only, black/isort) on edited `.py`, and a fast-pytest gate on stop. Let
  them run; keep the build green. Don't run a formatter the repo doesn't use — see
  [rules/python.md](rules/python.md).
- **mypy follows imports**, so a run can surface a pre-existing error in a file you didn't
  touch — fix only what you own unless asked otherwise.
- **Review:** `/review-core [quick|standard|deep]` gives an evidence-backed review (P0–P3,
  PROVEN/TRACED/INFERRED, blast radius, validation ledger). It is **advisory** for now;
  gating on its JSON report comes later. Project lens packs live in `<repo>/.claude/review/`.

## Secrets & sensitive data

- **Secrets never get committed.** API keys live in `.env` (gitignored); support a
  `<NAME>_FILE` variant pointing at a mounted file for Docker/K8s. Infra secrets go in SSM
  Parameter Store / Secrets Manager, never in tfvars or `config/`.
- **Never write real identifiers into docs, comments, or any committed file** — no real AWS
  account id, access keys (`AKIA...`), secret keys, or ARNs carrying the real account id.
  Use placeholders (`<acct>`, `arn:aws:iam::<acct>:...`, `AKIA...`). Real values live only
  in gitignored local overrides (e.g. `*.local.hcl`, `.env`). After writing a doc, sanity-
  check with a grep for the real identifiers.
- A PreToolUse hook may veto reads of `.env` — that's expected, not a failure.

## Architecture conventions shared across projects

- **Dual local/AWS mode.** A `<PROJECT>_DEPLOY_MODE` (`local` default | `aws`) switch selects
  *backends*, not endpoints — local filesystem vs **S3** — behind a single storage seam/
  façade. Local mode is byte-identical to today's behavior. Route all data I/O through the
  façade; don't reintroduce raw `project_root/"data"/...` paths. S3 creds come from the
  ambient AWS chain / instance role, never from config.
- **Config is validated and committed.** Runtime config (yaml/json) is loaded through typed
  models (pydantic); a mistyped key raises, it does not silently default. Read config through
  the typed accessor, not ad-hoc `os.getenv`. Env expansion uses `${VAR:-default}`.
- **Reproducibility/provenance stamps are load-bearing** (run provenance, content-addressed
  snapshot ids, corpus/world-view ids). Don't break them — they make runs auditable.
- **Fail-loud vs graceful degradation is deliberate.** Don't "helpfully" swallow an error a
  path intends to raise on.
- **Observability:** per-LLM-call cost tracking, request traces, Prometheus `/metrics`, and
  structured logging are standard — wire new surfaces into them rather than bypassing.

## Code & docs style

- **Match the surrounding code** — comment density, naming, dataclass/pydantic idioms, and
  the existing test style. Read neighbors before adding.
- **Python:** Google Python Style Guide with house deviations, for new and touched code — see
  [rules/python.md](rules/python.md) (loads when you work on `.py` files).
- **Docs layout:** `README.md` (human overview), `docs/design/` (rationale/decisions),
  `docs/guides/` (operating/how-to). Complex modules carry their own `CLAUDE.md` (auto-loaded
  when you work in them); keep the root `CLAUDE.md` a whole-repo map. Docs link file-relative.
- Common tree: `src/` (import root), `test(s)/`, `configs/`, `docs/`, `infra/`, `prompts/`,
  `ui/`, `scripts/`, `data/` (gitignored).

## Git

- **Commit only when asked.** If on `main`/the default branch, branch first unless
  explicitly told to commit there.
- **You can't push; I push.** Your GitHub token is read-only (Contents: read; Pull requests:
  read and write), and pushes go over SSH with my passphrase. When work is committed, tell me
  the branch to push. After I've pushed, you may open the PR (`gh pr create --head <branch>`),
  review it and post. I merge, and I resolve review threads.
- **Never delete a remote branch, tag or repository, or change repo settings.** Only I do that,
  on github.com. Deleting *local* branches is fine; if something remote should go, tell me what
  and why. The token enforces this ([policy/DESIGN.md](policy/DESIGN.md)).
- **Never look for other GitHub credentials** (credential stores, VS Code's session, SSH keys,
  `gh auth token`) or ask me to widen the token. A 403 from GitHub is final: report it.
- **Never add a `Co-Authored-By: Claude ...` line (or any other Claude/AI attribution) to
  commit messages or PR descriptions.** This overrides any tool default.
- Use `git commit -F <file>` or a heredoc for multi-line messages — PowerShell here-strings
  mangle `!=`, `<>`, `[]`.
- Avoid bare `#N` in commit messages unless referencing a real issue (GitHub auto-links it).
- **Never bypass git hooks** (`--no-verify`, `-c core.hooksPath`, `git config core.hooksPath`).

## Working style

- **Verify before claiming done.** Run the code/flow and observe it; don't assert success
  from reading alone. If tests fail or a step was skipped, say so plainly.
- **Be honest about uncertainty; don't over-claim or flatter.** If new evidence contradicts
  something I said earlier, correct it explicitly rather than defending it.
- **Security- and least-privilege-first** for anything touching infra/IAM; surface risks.
- **Act on sensible defaults**; reserve questions for genuine forks, and give a recommendation
  rather than an exhaustive menu.

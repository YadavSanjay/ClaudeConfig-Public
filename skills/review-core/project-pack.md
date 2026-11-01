# Project lens pack

A project plugs domain knowledge into review-core with a committed folder:

```
<repo>/.claude/review/
  profile.md          # required: manifest frontmatter (for review.py) + prose (for reviewers)
  lenses/<name>.md    # optional: domain lenses
  suppressions.md     # committed: dismissed finding ids (contract §6)
  codemap.json        # committed: TRUNK/BRANCH/LEAF per module (built by scripts/codemap.py)
  out/                # GITIGNORED: JSON reports + ledger.jsonl
```

If the repo ignores `.claude/`, narrow the rule so the pack is tracked: `.claude/*`,
`!.claude/review/`, `.claude/review/out/`.

## profile.md frontmatter (read by `scripts/review.py`)

Each value is JSON on one line; an indented line continues the previous key.

```markdown
---
python: "conda run --no-capture-output -n py312 python"
env: {"PYTHONPATH": "{repo}"}
gate: ["{python} -m ruff check {changed_py}", "{python} -m mypy", "{python} -m pytest -q {tests}"]
tests_map: {"src/pricer/**": ["tests/pricer"], "src/common/**": ["tests/common", "tests/pricer"]}
contract_surfaces: ["configs/pricer/golden_pvs.json", "src/common/provenance.py"]
hotspots: ["src/common/**"]
---
```

| Key | Used by | Meaning |
|---|---|---|
| `python` | prep (gate, packet) | Command prefix for Python, substituted as `{python}`. |
| `env` | prep (gate) | Extra env vars; `{repo}` is substituted. |
| `gate` | prep (gate) | Fast gate commands. `{changed_py}` = changed .py files; `{tests}` = test files that import the changed modules (via the code graph), else the `tests_map` dirs. A command whose placeholder resolves empty is skipped. Timeout is 300 s. |
| `tests_map` | prep (gate) | Fallback: changed-path glob → test dirs to run. |
| `gate_env_failures` | prep (gate) | Regexes. A failing gate whose output matches one is labelled an environment gap (e.g. data absent), not left unattributed. |
| `diff_test` | prep (deep) | Differential-test command; placeholders `{python} {junit} {tests} {repo}`. Default `{python} -m pytest -q -p no:cacheprovider --junitxml={junit} {tests}`. |
| `snapshot_links` | prep (deep) | Gitignored dirs that tests need (e.g. `["data"]`), linked into the base snapshot so base and change runs are symmetric. The links are removed before the snapshot is deleted. |
| `contract_surfaces` | prep (blast map, deep gate) | Globs whose change means blast `CONTRACT`; touching one also qualifies a change for `deep`. |
| `hotspots` | prep (blast map) | Globs for modules many subsystems import. |
| `criticality` | codemap.py build | What the system exists to produce. It drives TRUNK/BRANCH/LEAF; see below. |

## Code map: criticality, tiers, refresh

`criticality` declares **outputs**; the engine computes everything upstream of them.

```
criticality: {"outputs": {"pv": ["src/pricer/engine.py"], "risk": ["src/pricer/risk/var.py"]},
  "data_edges": ["src/marketdata/common/data_store.py -> src/pricer/loader/**"],  # file-mediated flow
  "non_value": ["src/common/obs/**"],              # on the path but can't change a number
  "pins": {"trunk": ["configs/pricer/golden_pvs.json"]}}
```

How tiers are assigned:
1. Pins win.
2. **TRUNK·value**: anything an output depends on, through imports plus `data_edges`, that
   isn't `non_value`.
3. **TRUNK·infra**: the top 10% by transitive fan-in among the rest. `non_value` is capped at
   BRANCH.
4. **LEAF**: nothing in production depends on it. UI and scripts are always LEAF.
5. **BRANCH**: everything else. A LEAF that co-changes 3 or more times with a TRUNK file is
   lifted to BRANCH.

Every module gets a one-line reason. Empty package `__init__` files are left out.

**Workflow** (one time per repo, repeated after broad refactors or architecture changes):
1. `codemap.py scan`: facts for drafting.
2. The orchestrating agent reads the CLAUDE.md files and drafts `criticality`.
3. **You approve it**, and it's written to the profile.
4. `codemap.py build`.
5. Review the tier list; disagreements become `pins`. `codemap.py explain <path>` shows one
   module's reasoning.

`prep` warns when the import graph has changed since the build. If TRUNK exceeds 40% of
production modules, `build` warns: tighten the declaration or accept it deliberately.

The **prose body** goes verbatim into the reviewer packet. Keep it short:
- **Data/runtime availability:** what needs gitignored data or services, and how to check.
- **Terminology:** renames and aliases a reviewer must not flag.
- **Exclusions:** project-wide non-findings.
- Anything about contract surfaces that a glob can't express.

## Lens file format

```markdown
---
name: numerics
applies_to: ["src/pricer/**/*.py", "src/sg/{a,b}.py"]   # globs on changed paths; "**" = always
---
# <Lens title>
## Hunt for        — concrete defect patterns, most damaging first
## Severity anchors — domain examples mapped to P0–P3 (never redefine the scale)
## Proof recipes   — cheap commands/scripts that turn a suspicion into PROVEN
## Not findings    — deliberate choices / known exceptions to skip
```

Globs support `*`, `?`, `**` and `{a,b}` alternation. A pack lens with the **same name** as a
global lens replaces it for that repo; otherwise lenses add up. The global lens is
`change-integrity`, which runs on everything.

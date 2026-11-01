# Rubric (condensed from contract.md — that file wins on any conflict)

**Severity** — consequence × likelihood if it ships:
- **P0** Blocker: wrong results reach an output silently; persisted data corrupted/lost; provenance broken; secret committed.
- **P1** Major: wrong on a realistic path but visible/bounded; fail-loud contract now swallows; contract surface changed without lockstep; tests weakened to pass; unexplained baseline drift.
- **P2** Minor: rare/edge path; untested behavior change on a non-critical path; perf/cost regression; misleading doc about changed code.
- **P3** Nit: clarity a senior engineer would still mention. Never at INFERRED.

**Evidence** — strongest actually obtained (no confidence %):
- **PROVEN**: a command you ran demonstrated the *failure* (cite command + exit/output). A grep, scan or passing test only supports TRACED.
- **TRACED**: `file:line → file:line → wrong effect` chain followed in code.
- **INFERRED**: reasoning only; say what you did not verify.

**Blast** — use the blast map you were given, don't guess:
**LOCAL** (private) · **MODULE** (same package) · **CROSS** (another subsystem) · **CONTRACT** (persisted data/schema, API, config/env contract, provenance, golden baselines, external consumer — profile lists them).
Each symbol's `reach` and `path` in the map are measured through wrappers (up to 3 hops); cite the path. The **tier** (TRUNK/BRANCH/LEAF) of a touched file says how central it is — read TRUNK changes harder, but tier never changes severity.

**Rules**: ≤ 2 sentences per field. Only lines the change touched, unless it makes old code newly reachable ("pre-existing, now reachable"). Skip what a linter/type-checker catches, stated non-findings, and suppressed ids. Fewer, stronger findings; an empty list with an honest `checked` list is a good result.

**Return** one ```json object: `{"findings":[{id, severity, evidence, blast, lens, file, line, symbol, summary, failure, fix, proof}], "checked":[...], "not_checked":[...]}` — `id` = `<lens>:<path>:<symbol>:<kebab-slug>` (no line number).

"""Code graph + domain-aware code map for review-core (stdlib only).

  codemap.py build   --repo R   write <repo>/.claude/review/codemap.json, print a tier summary
  codemap.py explain --repo R X tier, reason, metrics for a path or dotted module
  codemap.py scan    --repo R   facts for drafting the profile's `criticality` block (writes nothing)

Tiers come from a committed declaration in profile.md frontmatter, not from reference counts
alone:
  criticality: {"outputs": {"pv": ["src/pricer/engine.py"]},
                "data_edges": ["src/marketdata/** -> src/pricer/loader/**"],
                "non_value": ["src/common/obs/**"],
                "pins": {"trunk": [...], "branch": [...], "leaf": [...]}}
TRUNK·value = transitive dependencies of an output (imports + data edges, minus non_value);
TRUNK·infra = top-10% transitive fan-in among the rest; LEAF = nothing in production depends
on it (or script/ui); BRANCH = the rest. Pins win. Every module records a one-line reason.

`review.py` uses build_graph() + callers() for the transitive blast map and test selection.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review import _subsystem, dump_json, git, glob_match, load_json, load_profile

SCHEMA = "review-core-codemap/1"
MAX_DEPTH, MAX_NODES, AMBIGUOUS_DEFS = 3, 40, 5
SOURCE_ROOTS = ("", "src.")       # absolute imports, then src-layout repos run with PYTHONPATH=src
SKIP_PREFIXES = (".claude/",)     # tooling (hooks), not part of the system under review
ROUTE_PREFIXES = ("app.", "router.")


# =========================================================================== graph
@dataclass
class Def:
    path: str
    module: str
    qualname: str
    start: int
    end: int
    is_root: bool = False                  # route handler / CLI main in a __main__ module


@dataclass
class Graph:
    repo: Path
    kinds: dict[str, str] = field(default_factory=dict)          # module -> prod|test|script|ui
    paths: dict[str, str] = field(default_factory=dict)          # module -> path
    imports: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    defs: dict[str, list[Def]] = field(default_factory=lambda: defaultdict(list))        # by file
    by_name: dict[str, list[Def]] = field(default_factory=lambda: defaultdict(list))
    refs: dict[str, list[tuple[str, int]]] = field(default_factory=lambda: defaultdict(list))
    main_modules: set[str] = field(default_factory=set)
    exports: dict[str, set[str]] = field(default_factory=dict)   # module -> __all__
    empty_inits: set[str] = field(default_factory=set)           # packages whose __init__ is empty
    untracked: set[str] = field(default_factory=set)             # modules from `extra` (untracked) files

    def module_of(self, path: str) -> str | None:
        mod = module_name(path)
        return mod if mod in self.kinds else None

    def dependents(self, mod: str) -> set[str]:
        return {m for m, deps in self.imports.items() if mod in deps}

    def edge_hash(self, tracked_only: bool = False) -> str:
        """Hash of the non-test import edges. `tracked_only` leaves out untracked modules, so a map built
        from tracked files isn't reported stale just because a working-tree review adds new files."""
        skip = self.untracked if tracked_only else set()
        edges = sorted(f"{a}>{b}" for a, deps in self.imports.items()
                       if self.kinds[a] != "test" and a not in skip for b in deps if b not in skip)
        return hashlib.sha256("\n".join(edges).encode()).hexdigest()[:16]


def module_name(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def kind_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if path.startswith("tests/") or "/tests/" in path or name.startswith("test_") or name == "conftest.py":
        return "test"
    if path.startswith("scripts/"):
        return "script"
    if path.startswith("ui/"):
        return "ui"
    return "prod"


def _resolve_import(g: Graph, mod: str, is_pkg: bool, node: ast.Import | ast.ImportFrom) -> set[str]:
    def known(name: str) -> str | None:
        for root in SOURCE_ROOTS:                        # `from utils.x` in a src-layout repo is src.utils.x
            cand = root + name
            while cand and cand != root.rstrip("."):
                if cand in g.kinds:
                    return cand
                cand = cand.rpartition(".")[0]
        return None

    found: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if (k := known(alias.name)) is not None:
                found.add(k)
        return found
    if node.level:
        pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
        pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
        base = ".".join(pkg + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    for alias in node.names:
        k = known(f"{base}.{alias.name}") or known(base)
        if k is not None:
            found.add(k)
    return found


def build_graph(repo: Path, extra: list[str] | None = None) -> Graph:
    """Parse tracked .py files (+ `extra`, e.g. untracked files in a working-tree scope)."""
    g = Graph(repo=repo)
    files = [p for p in git(repo, "ls-files", "*.py").splitlines() if p and not p.startswith(SKIP_PREFIXES)]
    tracked = set(files)
    files += [p for p in extra or [] if p.endswith(".py") and p not in files and not p.startswith(SKIP_PREFIXES)]
    trees: dict[str, ast.Module] = {}
    for path in files:
        try:
            trees[path] = ast.parse((repo / path).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):     # OSError: tracked but deleted in the working tree
            continue
        mod = module_name(path)
        g.kinds[mod], g.paths[mod] = kind_of(path), path
        if path not in tracked:
            g.untracked.add(mod)
    for path, tree in trees.items():
        mod = module_name(path)
        is_pkg = path.endswith("__init__.py")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                g.imports[mod] |= _resolve_import(g, mod, is_pkg, node) - {mod}
            elif isinstance(node, ast.Name):
                g.refs[node.id].append((path, node.lineno))
            elif isinstance(node, ast.Attribute):
                g.refs[node.attr].append((path, node.lineno))
        for stmt in tree.body:
            if (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare)
                    and isinstance(stmt.test.left, ast.Name) and stmt.test.left.id == "__name__"):
                g.main_modules.add(mod)
            is_all = isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
            if is_all and isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.List, ast.Tuple)):
                g.exports[mod] = {e.value for e in stmt.value.elts
                                  if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        _collect_defs(g, path, mod, tree.body, "")
        if is_pkg and all(isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) for s in tree.body):
            g.empty_inits.add(mod)                       # only a docstring (or nothing): not a real module
    for path, defs in g.defs.items():
        mod = module_name(path)
        for d in defs:
            if mod in g.main_modules and d.qualname == "main":
                d.is_root = True
    return g


def _collect_defs(g: Graph, path: str, mod: str, body: list[ast.stmt], prefix: str) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qual = f"{prefix}{node.name}"
            decos = [ast.unparse(d) for d in getattr(node, "decorator_list", [])]
            d = Def(path, mod, qual, node.lineno, node.end_lineno or node.lineno,
                    is_root=any(s.startswith(ROUTE_PREFIXES) for s in decos))
            g.defs[path].append(d)
            g.by_name[node.name].append(d)
            _collect_defs(g, path, mod, node.body, qual + ".")


def enclosing(g: Graph, path: str, line: int) -> Def | None:
    """Innermost function/class def in `path` containing `line`."""
    best = None
    for d in g.defs.get(path, []):
        if d.start <= line <= d.end and (best is None or d.start >= best.start):
            best = d
    return best


# =========================================================================== callers
def callers(g: Graph, path: str, symbol: str, contract_globs: list[str],
            depth: int = MAX_DEPTH, cap: int = MAX_NODES) -> dict[str, Any]:
    """Transitive referencers of `symbol` defined in `path`, hop by hop through enclosing defs."""
    origin = (path, symbol)
    parent: dict[tuple[str, str], tuple[str, str] | None] = {origin: None}
    hop: dict[tuple[str, str], int] = {origin: 0}
    nodes: list[dict[str, Any]] = []
    tests: set[str] = set()
    ambiguous: set[str] = set()
    queue = deque([origin])
    while queue and len(nodes) < cap:
        cur = queue.popleft()
        if hop[cur] >= depth:
            continue
        cur_path, cur_qual = cur
        name = cur_qual.rsplit(".", 1)[-1]
        if name.startswith("__") and name.endswith("__"):
            continue                                   # dunders: too generic to follow
        prod_defs = [d for d in g.by_name.get(name, []) if kind_of(d.path) == "prod"]
        if len(prod_defs) > 1:
            ambiguous.add(name)
        if len(prod_defs) > AMBIGUOUS_DEFS:
            continue                                   # common name: references are mostly noise
        private = name.startswith("_")
        own = next((d for d in g.defs.get(cur_path, [])
                    if d.qualname == cur_qual or d.qualname.endswith("." + cur_qual)), None)
        for ref_path, line in g.refs.get(name, []):
            if private and ref_path != cur_path:
                continue
            if own and ref_path == cur_path and own.start <= line <= own.end:
                continue                               # recursion / its own body
            if kind_of(ref_path) == "test":
                tests.add(ref_path)
                continue
            enc = enclosing(g, ref_path, line)
            key = (ref_path, enc.qualname if enc else "<module>")
            if key in parent:
                continue
            parent[key], hop[key] = cur, hop[cur] + 1
            mod = module_name(ref_path)
            is_root = bool(enc and enc.is_root) or (enc is None and mod in g.main_modules)
            nodes.append({"file": ref_path, "qualname": key[1], "hop": hop[key], "is_root": is_root,
                          "contract": glob_match(ref_path, contract_globs), "subsystem": _subsystem(ref_path)})
            if key[1] != "<module>":
                queue.append(key)
            if len(nodes) >= cap:
                break

    def chain(n: dict[str, Any]) -> list[str]:
        out: list[str] = []
        k: tuple[str, str] | None = (n["file"], n["qualname"])
        while k is not None:
            out.append(f"{module_name(k[0]).rsplit('.', 1)[-1]}.{k[1]}" if k[1] != "<module>" else module_name(k[0]))
            k = parent[k]
        return out[::-1]

    origin_sub = _subsystem(path)
    reach, why = "LOCAL", None
    ranked = (("CONTRACT", lambda n: n["is_root"] or n["contract"]),
              ("CROSS", lambda n: n["subsystem"] != origin_sub),
              ("MODULE", lambda n: n["file"] != path))
    for level, test in ranked:
        hit = next((n for n in nodes if test(n)), None)
        if hit:
            reach, why = level, hit
            break
    if reach == "LOCAL" and glob_match(path, contract_globs):
        reach = "CONTRACT"
    return {"reach": reach, "hops": max((n["hop"] for n in nodes), default=0), "nodes": len(nodes),
            "capped": len(nodes) >= cap, "path": " ← ".join(chain(why)) if why else symbol,
            "ambiguous": sorted(ambiguous), "tests": len(tests),
            "by_subsystem": dict(Counter(n["subsystem"] for n in nodes)),
            "sample": [f"{n['file']}:{n['qualname']} (hop {n['hop']}{', root' if n['is_root'] else ''})"
                       for n in nodes[:12]],
            "nodes_detail": [{**n, "path": " ← ".join(chain(n))} for n in nodes]}


def importing_tests(g: Graph, modules: set[str]) -> list[str]:
    """Test files that import any of `modules`, directly or through non-test_ test helpers."""
    helpers = {m for m, k in g.kinds.items() if k == "test" and not g.paths[m].rsplit("/", 1)[-1].startswith("test_")}
    reach = set(modules)
    changed = True
    while changed:                                     # helpers that (transitively) import a target
        changed = False
        for h in helpers - reach:
            if g.imports.get(h, set()) & reach:
                reach.add(h)
                changed = True
    return sorted(g.paths[m] for m, k in g.kinds.items()
                  if k == "test" and g.paths[m].rsplit("/", 1)[-1].startswith("test_")
                  and g.imports.get(m, set()) & reach)


# =========================================================================== tiers
def _pagerank(nodes: list[str], deps: dict[str, set[str]], iters: int = 40, d: float = 0.85) -> dict[str, float]:
    n = len(nodes) or 1
    pr = dict.fromkeys(nodes, 1 / n)
    for _ in range(iters):
        nxt = dict.fromkeys(nodes, (1 - d) / n)
        dangling = sum(pr[m] for m in nodes if not deps[m])
        for m in nodes:
            out = deps[m]
            for t in out:
                nxt[t] += d * pr[m] / len(out)
        for m in nodes:
            nxt[m] += d * dangling / n
        pr = nxt
    return pr


def _cochange(repo: Path, tracked: set[str], commits: int = 300, max_files: int = 30) -> dict[str, Counter[str]]:
    pairs: dict[str, Counter[str]] = defaultdict(Counter)
    log = git(repo, "log", f"-n{commits}", "--name-only", "--pretty=format:@@", check=False)
    for block in log.split("@@"):
        files = sorted({f for f in block.split() if f in tracked})
        if 1 < len(files) <= max_files:
            for a in files:
                for b in files:
                    if a != b:
                        pairs[a][b] += 1
    return pairs


def build_map(repo: Path, g: Graph | None = None) -> dict[str, Any]:
    g = g or build_graph(repo)
    meta, _ = load_profile(repo)
    crit = meta.get("criticality", {}) or {}
    outputs: dict[str, list[str]] = crit.get("outputs", {})
    non_value: list[str] = crit.get("non_value", [])
    pins: dict[str, list[str]] = crit.get("pins", {})
    tracked = [p for p in git(repo, "ls-files").splitlines() if p]

    prod = sorted(m for m, k in g.kinds.items() if k == "prod")
    deps: dict[str, set[str]] = {m: {t for t in g.imports.get(m, set()) if g.kinds.get(t) == "prod"} for m in prod}
    # data edges: "A-glob -> B-glob" means files matching B consume what A produces (B depends on A)
    data_nodes: dict[str, str] = {}
    for edge in crit.get("data_edges", []):
        src, _, dst = (s.strip() for s in edge.partition("->"))
        sources = [module_name(p) if p.endswith(".py") else p for p in tracked if glob_match(p, src)]
        for s in sources:
            if s not in g.kinds and s not in deps:      # non-.py source (config/data file)
                data_nodes[s] = s
                deps[s] = set()
        for m in prod:
            if glob_match(g.paths[m], dst):
                deps[m] |= {s for s in sources if s in deps and s != m}
    nodes = sorted(deps)
    path_of = {**{m: g.paths[m] for m in prod}, **data_nodes}

    dependents: dict[str, set[str]] = defaultdict(set)
    for m, ds in deps.items():
        for t in ds:
            dependents[t].add(m)

    def transitive(start: str, edges: dict[str, set[str]]) -> set[str]:
        seen, todo = set(), [start]
        while todo:
            for nxt in edges.get(todo.pop(), set()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    pr = _pagerank(nodes, deps)
    test_in = Counter(t for m, k in g.kinds.items()                     # direct: imported by a test_ file
                      if k == "test" and g.paths[m].rsplit("/", 1)[-1].startswith("test_")
                      for t in g.imports.get(m, set()))
    test_reach = test_reach_counts(g)
    metrics: dict[str, dict[str, Any]] = {m: {
                   "fan_in": len(dependents[m]), "fan_in_transitive": len(transitive(m, dependents)),
                   "fan_out": len(deps[m]), "pagerank": round(pr[m] * len(nodes), 3), "tests": test_in.get(m, 0),
                   "tests_reach": test_reach.get(m, 0),
                   "test_coverage": ("direct" if test_in.get(m) else "indirect" if test_reach.get(m) else "none")}
               for m in nodes}
    for m in nodes:
        fi, fo = metrics[m]["fan_in"], metrics[m]["fan_out"]
        metrics[m]["instability"] = round(fo / (fi + fo), 2) if fi + fo else 0.0

    # value path: BFS from each output module along dependencies, not through non_value
    value: dict[str, dict[str, Any]] = {}
    for out_name, globs in outputs.items():
        starts = [m for m in nodes if glob_match(path_of[m], [s.split(":", 1)[0] for s in globs])]
        par: dict[str, str | None] = dict.fromkeys(starts)
        dist = dict.fromkeys(starts, 0)
        q = deque(starts)
        while q:
            cur = q.popleft()
            if glob_match(path_of[cur], non_value):
                continue
            v = value.setdefault(cur, {"outputs": set(), "distance": dist[cur], "chain": None})
            v["outputs"].add(out_name)
            if dist[cur] <= v["distance"]:
                chain: list[str] = []
                k: str | None = cur
                while k is not None:
                    chain.append(k.rsplit(".", 1)[-1] if "/" not in k else k)
                    k = par[k]
                v["distance"], v["chain"] = dist[cur], " ← ".join(chain)
            for nxt in deps[cur]:
                if nxt not in dist:
                    dist[nxt], par[nxt] = dist[cur] + 1, cur
                    q.append(nxt)

    rest = [m for m in prod if m not in value and metrics[m]["fan_in_transitive"] > 0
            and not glob_match(g.paths[m], non_value)]
    cutoff = sorted((metrics[m]["fan_in_transitive"] for m in rest), reverse=True)
    infra_min = cutoff[max(0, len(cutoff) // 10 - 1)] if cutoff else 10**9
    cochange = _cochange(repo, set(path_of.values()))

    modules: dict[str, dict[str, Any]] = {}
    for m in nodes:
        if m in g.empty_inits:
            continue                                     # empty package __init__: noise, not a module
        p = path_of[m]
        pinned = next((t for t, gl in pins.items() if glob_match(p, gl)), None)
        fan_in_t = metrics[m]["fan_in_transitive"]
        if pinned:
            tier, reason = pinned.upper(), "pinned in profile criticality.pins"
        elif m in value and not glob_match(p, non_value):
            v = value[m]
            tier = "TRUNK"
            reason = f"TRUNK·value → {', '.join(sorted(v['outputs']))} ({v['distance']} hops): {v['chain']}"
        elif m in g.kinds and fan_in_t >= infra_min and fan_in_t > 0 and not glob_match(p, non_value):
            tier, reason = "TRUNK", f"TRUNK·infra: {fan_in_t} transitive dependents (top 10%)"
        elif fan_in_t and glob_match(p, non_value):
            tier, reason = "BRANCH", f"non_value (capped at BRANCH): {fan_in_t} transitive dependents"
        elif metrics[m]["fan_in_transitive"] == 0:
            tier, reason = "LEAF", "no production module depends on it"
        else:
            tier, reason = "BRANCH", f"{metrics[m]['fan_in_transitive']} transitive dependents, not on a value path"
        modules[m] = {"path": p, "tier": tier, "reason": reason, "metrics": metrics[m],
                      "outputs": sorted(value[m]["outputs"]) if m in value else [],
                      "distance": value[m]["distance"] if m in value else None,
                      "cochange": [f"{q} ({n})" for q, n in cochange.get(p, Counter()).most_common(3) if n >= 2]}
    trunk_paths = {v["path"] for v in modules.values() if v["tier"] == "TRUNK"}
    for v in modules.values():                          # light co-change lift: LEAF -> BRANCH
        if v["tier"] == "LEAF":
            hot = [(p, c) for p, c in cochange.get(v["path"], Counter()).items() if p in trunk_paths and c >= 3]
            if hot:
                partner, count = max(hot, key=lambda x: x[1])
                v["tier"], v["reason"] = "BRANCH", f"co-changes with TRUNK {partner} ({count} commits)"
    for m, k in g.kinds.items():                        # ui / scripts: always leaves
        if k in ("ui", "script") and m not in g.empty_inits:
            modules[m] = {"path": g.paths[m], "tier": "LEAF", "reason": f"{k} code (consumer, not depended on)",
                          "metrics": {}, "outputs": [], "distance": None, "cochange": []}
    return {"schema": SCHEMA, "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="minutes"),
            "head_sha": git(repo, "rev-parse", "HEAD").strip(), "edge_hash": g.edge_hash(),
            "declared": bool(outputs), "modules": dict(sorted(modules.items()))}


def test_reach_counts(g: Graph) -> Counter[str]:
    """For each production module, how many test files reach it: a test's own imports, followed
    transitively through test helpers and production imports (code-level reach, not coverage %)."""
    reach: Counter[str] = Counter()
    for t, kind in g.kinds.items():
        if kind != "test" or not g.paths[t].rsplit("/", 1)[-1].startswith("test_"):
            continue
        seen: set[str] = set()
        todo = list(g.imports.get(t, set()))
        while todo:
            m = todo.pop()
            if m in seen or g.kinds.get(m) not in ("prod", "test"):
                continue
            seen.add(m)
            todo.extend(g.imports.get(m, set()))
        reach.update(m for m in seen if g.kinds[m] == "prod")
    return reach


def summary(cmap: dict[str, Any]) -> dict[str, Any]:
    mods = cmap["modules"]
    counts = Counter(v["tier"] for v in mods.values())
    py = [v for v in mods.values() if v["path"].endswith(".py") and v["metrics"]]      # code, not data files
    prod_trunk = [v for v in py if v["tier"] == "TRUNK"]
    total = len(py) or 1
    untested = sorted(v["path"] for v in prod_trunk if v["metrics"].get("test_coverage") == "none")
    out: dict[str, Any] = {"tiers": dict(counts), "trunk_share": round(len(prod_trunk) / total, 2),
                           "trunk_test_coverage": dict(Counter(v["metrics"].get("test_coverage", "none")
                                                               for v in prod_trunk)),
                           "trunk_untested": untested[:15], "trunk_untested_count": len(untested)}
    if not cmap["declared"]:
        out["warning"] = "no criticality.outputs declared: tiers are structural only (run the proposal step)"
    elif out["trunk_share"] > 0.4:
        out["warning"] = "TRUNK > 40% of modules: tighten criticality.non_value / outputs"
    return out


def map_path(repo: Path) -> Path:
    return repo / ".claude" / "review" / "codemap.json"


def load_map(repo: Path) -> dict[str, Any] | None:
    p = map_path(repo)
    return load_json(p) if p.exists() else None


# =========================================================================== cli
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "explain", "scan"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    if a.cmd == "build":
        cmap = build_map(repo)
        dump_json(cmap, map_path(repo))
        print(json.dumps({"written": str(map_path(repo)), **summary(cmap)}, indent=1))
    elif a.cmd == "explain":
        built = load_map(repo)
        if not built or not a.target:
            sys.exit("explain: needs a built codemap.json and a target path/module")
        key = a.target if a.target in built["modules"] else module_name(a.target.replace("\\", "/"))
        print(json.dumps(built["modules"].get(key, {"error": f"{a.target} not in map"}), indent=1))
    else:
        g = build_graph(repo)
        meta, _ = load_profile(repo)
        tracked = git(repo, "ls-files").splitlines()
        prod = [m for m, k in g.kinds.items() if k == "prod"]
        deps = {m: {t for t in g.imports.get(m, set()) if t in prod} for m in prod}
        pr = _pagerank(sorted(prod), defaultdict(set, deps))
        routes = Counter(d.path for ds in g.defs.values() for d in ds if d.is_root and d.qualname != "main")
        print(json.dumps({
            "claude_md": [p for p in tracked if p.endswith("CLAUDE.md")],
            "design_docs": [p for p in tracked if p.startswith("docs/design/") and p.endswith(".md")],
            "route_files": dict(routes.most_common()),
            "cli_modules": sorted(g.paths[m] for m in g.main_modules if g.kinds.get(m) != "test"),
            "top_pagerank": [g.paths[m] for m, _ in sorted(pr.items(), key=lambda x: -x[1])[:15]],
            "current_criticality": meta.get("criticality"),
        }, indent=1))


if __name__ == "__main__":
    main()

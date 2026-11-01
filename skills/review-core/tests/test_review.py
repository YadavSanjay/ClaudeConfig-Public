"""Tests for scripts/review.py — a temp git repo with a pack, no network."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import review as rv  # noqa: E402

PROFILE = """---
python: "python"
gate: ["{python} -c \\"print('lint ok')\\" {changed_py}", "{python} -c \\"import sys; sys.exit(0)\\" {tests}"]
tests_map: {"pkg/**": ["tests/pkg"]}
contract_surfaces: ["pkg/api.py"]
hotspots: ["pkg/**"]
---
# Profile
Terminology: none.
"""

LENS = """---
name: domain
applies_to: ["pkg/{core,api}.py"]
---
# Domain lens
## Hunt for
- domain things
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit and a pack, then an uncommitted change."""
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "__init__.py").write_text("")
    (r / "pkg" / "core.py").write_text("def price(x):\n    return x * 2\n\n\ndef _helper(y):\n    return y\n")
    (r / "pkg" / "api.py").write_text("from pkg.core import price\n\n\ndef handler(v):\n    return price(v)\n")
    (r / "app.py").write_text("import pkg.core\n\nprint(pkg.core.price(1))\n")
    pack = r / ".claude" / "review"
    (pack / "lenses").mkdir(parents=True)
    (pack / "profile.md").write_text(PROFILE)
    (pack / "lenses" / "domain.md").write_text(LENS)
    (pack / "suppressions.md").write_text(
        "# s\n- domain:pkg/api.py:handler:old-thing — wontfix — reason — 2026-09-30\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    (r / "pkg" / "core.py").write_text(
        "def price(x, scale=3):\n    return x * scale\n\n\ndef _helper(y):\n    return y + 1\n")
    (r / "pkg" / "extra.py").write_text("def new_fn():\n    return 1\n")
    return r


# ------------------------------------------------------------------ helpers
def test_frontmatter_parses_wrapped_json_and_strings() -> None:
    meta, body = rv.split_frontmatter('---\nname: x\napplies_to: ["a/**",\n  "b/*.py"]\n---\n# Body\n')
    assert meta == {"name": "x", "applies_to": ["a/**", "b/*.py"]}
    assert body.startswith("# Body")


def test_brace_glob_and_double_star() -> None:
    assert rv.expand_braces("s/{a,b}/{x,y}.py") == ["s/a/x.py", "s/a/y.py", "s/b/x.py", "s/b/y.py"]
    assert rv.glob_match("src/pricer/ql/x.py", "src/pricer/**/*.py")
    assert rv.glob_match("src/pricer/x.py", "src/pricer/**/*.py")
    assert not rv.glob_match("src/pricerx/x.py", "src/pricer/**")
    assert rv.glob_match("src/sg/arag/mapping.py", "src/sg/{arag,macro}/{calibrate,mapping}.py")
    assert rv.glob_match("anything/at/all", "**")


def test_rename_notation_keeps_directory() -> None:
    assert rv.new_path("tests/bff/{test_news.py => test_bff_news.py}") == "tests/bff/test_bff_news.py"
    assert rv.new_path("src/{old => new}/m.py") == "src/new/m.py"
    assert rv.new_path("src/{ => sub}/m.py") == "src/sub/m.py"
    assert rv.new_path("a.py => b.py") == "b.py"


# ------------------------------------------------------------------ prep pieces
def test_scope_dirty_default_branch_includes_untracked(repo: Path) -> None:
    s = rv.resolve_scope(repo, None, False)
    paths = {f["path"]: f for f in s["files"]}
    assert s["working_tree"] and s["diff_args"] == ["diff", "HEAD"]
    assert "pkg/core.py" in paths and paths["pkg/extra.py"].get("untracked")


def test_scope_single_commit_target(repo: Path) -> None:
    _git(repo, "commit", "-qam", "change")
    s = rv.resolve_scope(repo, "HEAD", False)
    assert [f["path"] for f in s["files"]] == ["pkg/core.py"]
    assert s["diff_args"][2] == s["head_sha"]


def test_select_lenses_globs_and_pack(repo: Path) -> None:
    chosen = {lens["name"]: lens for lens in rv.select_lenses(rv.resolve_scope(repo, None, False))}
    assert set(chosen) == {"change-integrity", "domain"}
    assert chosen["domain"]["source"] == "pack" and chosen["domain"]["matched"] == ["pkg/core.py"]


def test_blast_map_symbols_modules_and_surfaces(repo: Path) -> None:
    b = rv.blast_map(rv.resolve_scope(repo, None, False))
    syms = {(e["file"], e["symbol"]): e for e in b["symbols"]}
    price = syms[("pkg/core.py", "price")]
    assert not price["private"] and price["usages"] >= 2                 # api.handler + app module
    assert price["reach"] == "CONTRACT" and "api.handler" in price["path"]  # api.py is a contract surface
    assert syms[("pkg/core.py", "_helper")]["private"]
    assert ("pkg/extra.py", "new_fn") in syms                             # untracked file
    assert {m["module"]: m for m in b["modules"]}["pkg.core"]["importer_files"] == 2
    assert b["contract_surfaces_touched"] == [] and "pkg/core.py" in b["hotspots_touched"]
    assert b["codemap"]["present"] is False and b["tiers"]["pkg/core.py"]["tier"] == "UNMAPPED"


# ------------------------------------------------------------------ code map + transitive reach
import codemap as cm  # noqa: E402

GRAPH_FILES = {
    "src/__init__.py": "",
    "src/core/__init__.py": "",
    "src/core/engine.py": ("from src.common.storage import save\nfrom src.obs.log import log\n\n\n"
                           "def _core(x):\n    return x * 2\n\n\nclass Engine:\n"
                           "    def run(self, x):\n        log()\n        return save(_core(x))\n"),
    "src/core/util.py": "def run():\n    return 0\n",
    "src/common/__init__.py": "",
    "src/common/storage.py": "def save(v):\n    return v\n",
    "src/common/helpers.py": "def fmt(v):\n    return str(v)\n",
    "src/obs/__init__.py": "",
    "src/obs/log.py": "def log():\n    return None\n",
    "src/feed/__init__.py": "",
    "src/feed/ingest.py": "def ingest():\n    return 'csv'\n",
    "src/risk/__init__.py": "",
    "src/risk/calc.py": ("from src.core.engine import Engine\nfrom src.common.helpers import fmt\n\n\n"
                         "def compute(v):\n    return fmt(Engine().run(v))\n\n\n"
                         "def ping(n):\n    return pong(n - 1) if n else 0\n\n\n"
                         "def pong(n):\n    return ping(n - 1) if n else 0\n"),
    "src/risk/report.py": "from src.risk.calc import compute\nfrom src.common.helpers import fmt\n\n\n"
                          "def report():\n    return fmt(compute(1))\n",
    "src/api/__init__.py": "",
    "src/api/service.py": ("from src.risk.calc import compute\nfrom src.common.helpers import fmt\n\napp = None\n\n\n"
                           "@app.get('/x')\ndef handler(v):\n    return fmt(compute(v))\n"),
    "src/cli.py": ("from src.risk.calc import compute\nfrom src.common.helpers import fmt\n\n\n"
                   "def main():\n    print(fmt(compute(1)))\n\n\nif __name__ == '__main__':\n    main()\n"),
    "scripts/tool.py": "from src.core.engine import Engine\n\nEngine().run(1)\n",
    "tests/__init__.py": "",
    "tests/helpers.py": "from src.risk.calc import compute\n\n\ndef calc1():\n    return compute(1)\n",
    "tests/test_engine.py": ("from src.core.engine import Engine\n\n\n"
                             "def test_run():\n    assert Engine().run(1) == 2\n"),
    "tests/test_calc.py": "from tests.helpers import calc1\n\n\ndef test_calc():\n    assert calc1()\n",
    "configs/rates.json": "{}\n",
}

GRAPH_PROFILE = """---
python: "python"
gate: ["{python} -c \\"print('DATA_MISSING'); raise SystemExit(1)\\" {tests}"]
gate_env_failures: ["DATA_MISSING"]
tests_map: {"src/**": ["tests"]}
contract_surfaces: []
criticality: {"outputs": {"pv": ["src/core/engine.py:Engine.run"]},
  "data_edges": ["configs/** -> src/core/engine.py", "src/feed/ingest.py -> src/core/engine.py"],
  "non_value": ["src/obs/**"], "pins": {"branch": ["src/core/util.py"]}}
---
# profile
"""


@pytest.fixture
def graph_repo(tmp_path: Path) -> Path:
    r = tmp_path / "g"
    for rel, text in GRAPH_FILES.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    (r / ".claude" / "review").mkdir(parents=True)
    (r / ".claude" / "review" / "profile.md").write_text(GRAPH_PROFILE)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    for i in range(3):                                   # report.py co-changes with TRUNK engine.py
        for rel in ("src/risk/report.py", "src/core/engine.py"):
            with (r / rel).open("a") as fh:
                fh.write(f"# touch {i}\n")
        _git(r, "commit", "-qam", f"co-change {i}")
    return r


def test_callers_transitive_reach_through_wrapper(graph_repo: Path) -> None:
    g = cm.build_graph(graph_repo)
    t = cm.callers(g, "src/core/engine.py", "_core", contract_globs=[])
    assert t["reach"] == "CONTRACT"                       # _core -> Engine.run -> compute -> route
    assert t["path"] == "engine._core ← engine.Engine.run ← calc.compute ← service.handler"
    assert t["tests"] >= 1 and "run" in t["ambiguous"]    # test caller counted, not reached
    assert t["hops"] == 3 and any("cli.main" in s or "src/cli.py:main" in s for s in t["sample"])


def test_callers_cycle_cap_and_locality(graph_repo: Path) -> None:
    g = cm.build_graph(graph_repo)
    cyc = cm.callers(g, "src/risk/calc.py", "ping", contract_globs=[])
    assert cyc["nodes"] <= 2 and cyc["reach"] in ("LOCAL", "MODULE")
    assert cm.callers(g, "src/core/engine.py", "_core", contract_globs=[], cap=1)["capped"]
    assert cm.callers(g, "src/core/engine.py", "_core", contract_globs=[], depth=1)["reach"] == "LOCAL"


def test_codemap_tiers_reasons_and_cochange(graph_repo: Path) -> None:
    mods = cm.build_map(graph_repo)["modules"]
    tier = {v["path"]: v["tier"] for v in mods.values()}
    assert tier["src/core/engine.py"] == "TRUNK" and "value → pv" in mods["src.core.engine"]["reason"]
    assert tier["src/common/storage.py"] == "TRUNK"                    # engine depends on it
    assert tier["src/feed/ingest.py"] == "TRUNK"                       # via data edge
    assert tier["configs/rates.json"] == "TRUNK"                       # non-.py data source
    assert tier["src/obs/log.py"] == "BRANCH" and "non_value" in mods["src.obs.log"]["reason"]  # capped
    assert tier["src/common/helpers.py"] == "TRUNK" and "infra" in mods["src.common.helpers"]["reason"]
    assert "src.core" not in mods and "src.risk" not in mods          # empty package __init__ dropped
    assert tier["src/core/util.py"] == "BRANCH" and "pinned" in mods["src.core.util"]["reason"]
    assert tier["src/api/service.py"] == "LEAF" and tier["scripts/tool.py"] == "LEAF"
    assert tier["src/risk/report.py"] == "BRANCH" and "co-changes" in mods["src.risk.report"]["reason"]
    assert tier["src/risk/calc.py"] == "BRANCH"


def test_codemap_staleness_and_blast_tiers(graph_repo: Path) -> None:
    rv.dump_json(cm.build_map(graph_repo), cm.map_path(graph_repo))
    (graph_repo / "src/core/engine.py").write_text(
        (graph_repo / "src/core/engine.py").read_text().replace("x * 2", "x * 3"))
    b = rv.blast_map(rv.resolve_scope(graph_repo, None, False))
    assert b["codemap"]["present"] and not b["codemap"]["stale"]
    assert b["tiers"]["src/core/engine.py"]["tier"] == "TRUNK" and b["reach"] == "CONTRACT"
    assert [e["symbol"] for e in b["symbols"]] == ["_core"]           # enclosing def of the changed line
    (graph_repo / "src/core/util.py").write_text("from src.common.helpers import fmt\n\n\ndef run():\n    return 0\n")
    assert rv.blast_map(rv.resolve_scope(graph_repo, None, False))["codemap"]["stale"]


def test_gate_selects_importing_tests_and_labels_env(graph_repo: Path) -> None:
    (graph_repo / "src/risk/calc.py").write_text((graph_repo / "src/risk/calc.py").read_text() + "\n# edit\n")
    s = rv.resolve_scope(graph_repo, None, False)
    res = rv.run_gate(s, timeout=60, graph=cm.build_graph(graph_repo))
    assert res["tests_selected_by"] == "tests importing the changed modules"
    assert "tests/test_calc.py" in res["results"][0]["cmd"] and "test_engine" not in res["results"][0]["cmd"]
    assert res["results"][0]["exit"] == 1 and "DATA_MISSING" in res["results"][0]["environment"]


def test_run_gate_substitutes_and_runs(repo: Path) -> None:
    res = rv.run_gate(rv.resolve_scope(repo, None, False), timeout=60)["results"]
    assert [r["exit"] for r in res] == [0, 0]
    assert "pkg/core.py" in res[0]["cmd"] and "tests/pkg" in res[1]["cmd"]


def test_gate_timeout_kills_tree_and_records_skip(repo: Path) -> None:
    profile = repo / ".claude" / "review" / "profile.md"
    profile.write_text(profile.read_text().replace(
        'gate: [', 'gate: ["{python} -c \\"import time; time.sleep(30)\\"", ', 1))
    res = rv.run_gate(rv.resolve_scope(repo, None, False), timeout=2)["results"]
    assert res[0]["exit"] is None and "timeout" in res[0]["skipped"]
    assert [r["exit"] for r in res[1:]] == [0, 0]                         # later commands still run


def test_gate_stdin_is_closed(repo: Path) -> None:
    """A command that reads stdin must see EOF immediately, never block."""
    profile = repo / ".claude" / "review" / "profile.md"
    profile.write_text(profile.read_text().replace(
        'gate: [', 'gate: ["{python} -c \\"import sys; sys.exit(len(sys.stdin.read()))\\"", ', 1))
    assert rv.run_gate(rv.resolve_scope(repo, None, False), timeout=20)["results"][0]["exit"] == 0


# ------------------------------------------------------------------ CLI end to end
def _finding(**kw):
    base = {"id": "change-integrity:pkg/core.py:price:default-changed", "severity": "P1",
            "evidence": "TRACED", "blast": "CROSS", "lens": "change-integrity", "file": "pkg/core.py",
            "line": 1, "symbol": "price", "summary": "Default scale changed.",
            "failure": "Callers double now triple.", "fix": "Keep 2.", "proof": "core.py:1 -> api.py:5"}
    return {**base, **kw}


def test_prep_then_report(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work = tmp_path / "work"
    rv.main(["prep", "--repo", str(repo), "--intent", "make price scalable", "--work", str(work)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["lenses"] == ["change-integrity", "domain"] and "packet.md" in summary["packets"]
    packet = (work / "packet.md").read_text()
    for section in ("## Rubric", "### Lens `domain`", "## Project profile", "## Gate already run",
                    "domain:pkg/api.py:handler:old-thing", "## Diff", "new_fn"):
        assert section in packet

    findings = tmp_path / "f.json"
    findings.write_text(json.dumps({"findings": [
        _finding(),
        _finding(id="domain:pkg/core.py:price:default-changed", lens="domain"),            # duplicate
        _finding(id="change-integrity:pkg/core.py:_helper:nit", severity="P3", evidence="INFERRED",
                 symbol="_helper"),                                                        # dropped
        _finding(id="domain:pkg/api.py:handler:old-thing", severity="P2", symbol="handler"),  # suppressed
        {"id": "bad"}],                                                                     # malformed
        "checked": ["callers of price"], "not_checked": ["data absent"]}))
    refuter = tmp_path / "r.json"
    refuter.write_text(json.dumps([{"id": "change-integrity:pkg/core.py:price:default-changed",
                                    "verdict": "downgraded", "severity": "P2", "reason": "x"}]))
    out = tmp_path / "out"
    rv.main(["report", "--work", str(work), "--findings", str(findings), "--refuter", str(refuter),
             "--out-dir", str(out)])
    captured = capsys.readouterr()
    report = json.loads((out / "latest.json").read_text())
    assert report["verdict"] == "SHIP-WITH-NOTES" and report["intent"] == "make price scalable"
    assert [(f["severity"], f["lens"], f["refuter"]) for f in report["findings"]] == \
        [("P2", "change-integrity+domain", "downgraded")]
    assert report["ledger"]["suppressed"] == 1 and report["ledger"]["refuter"]["downgraded"] == 1
    assert "malformed" in captured.err
    assert "### Validation ledger" in captured.out and "data absent" in captured.out
    assert len((out / "ledger.jsonl").read_text().splitlines()) == 1


def test_standard_report_suggests_deep_on_contract_reach(repo: Path, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate"])
    capsys.readouterr()
    (tmp_path / "f.json").write_text(json.dumps({"findings": [], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(tmp_path / "f.json"), "--out-dir", str(tmp_path / "o")])
    assert "reach CONTRACT — consider `deep`" in capsys.readouterr().out


def test_report_defaults_to_self_ignored_repo_dir_without_pack(repo: Path, tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    import shutil
    shutil.rmtree(repo / ".claude" / "review")                         # no pack: still lands in the repo
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate"])
    capsys.readouterr()
    (tmp_path / "f.json").write_text(json.dumps({"findings": [], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(tmp_path / "f.json")])
    out = repo / ".claude" / "review" / "out"
    assert (out / "latest.json").exists() and (out / ".gitignore").read_text().splitlines()[-1] == "*"
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True).stdout
    assert "out/" not in status                                        # the report dir ignores itself


# ------------------------------------------------------------------ contract parity
def test_rubric_scale_names_match_contract() -> None:
    contract = (SKILL_DIR / "contract.md").read_text(encoding="utf-8")
    rubric = (SKILL_DIR / "rubric.md").read_text(encoding="utf-8")
    for term in rv.SEV + rv.EVIDENCE + rv.BLAST:
        assert re.search(rf"\*\*{term}\*\*", contract), term
        assert re.search(rf"\*\*{term}\*\*", rubric), term


# ------------------------------------------------------------------ deep: qualification, impact, diff tests
def _prep(repo: Path, work: Path, *extra: str) -> dict:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(work), "--no-gate", *extra])
    return json.loads(buf.getvalue())


def _local_change(repo: Path) -> None:
    with (repo / "src/risk/report.py").open("a") as fh:          # unused private helper: LOCAL reach
        fh.write("\n\ndef _unused_fmt(x):\n    return x\n")


def test_deep_falls_back_when_not_qualified(graph_repo: Path, tmp_path: Path) -> None:
    _local_change(graph_repo)
    s = _prep(graph_repo, tmp_path / "w1", "--effort", "deep")         # no code map: reach-only decision
    assert s["effort"] == "standard" and s["deep"].startswith("deep requested → standard")
    assert "tier unavailable" in s["deep"] and not s["impact_targets"] and s["diff_tests"] is None
    assert not list((tmp_path / "w1").glob("packet-impact-*.md"))
    rv.dump_json(cm.build_map(graph_repo), cm.map_path(graph_repo))
    s2 = _prep(graph_repo, tmp_path / "w2", "--effort", "deep")         # BRANCH-only with a map: still falls back
    assert s2["effort"] == "standard" and "BRANCH" in s2["deep"]
    s3 = _prep(graph_repo, tmp_path / "w3", "--effort", "deep", "--force-deep")
    assert s3["effort"] == "deep" and s3["deep"].startswith("forced")


def test_deep_qualifies_targets_consumers_and_runs_diff_tests(graph_repo: Path, tmp_path: Path) -> None:
    rv.dump_json(cm.build_map(graph_repo), cm.map_path(graph_repo))
    eng = graph_repo / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 3"))           # TRUNK, reach CONTRACT
    before = rv.git(graph_repo, "status", "--porcelain")
    work = tmp_path / "w"
    s = _prep(graph_repo, work, "--effort", "deep")
    assert s["effort"] == "deep" and s["deep"].startswith("qualified") and "TRUNK" in s["deep"]
    subs = [t["subsystem"] for t in s["impact_targets"]]
    assert "src/api" in subs and "src/core" not in subs                # origin dir excluded
    packet = (work / "packet-impact-1.md").read_text()
    assert "## Consumer focus" in packet and "←" in packet
    diff = json.loads((work / "diff_tests.json").read_text())
    assert diff["verdict"] == "behaviour change observed"              # test_run: passed -> failed
    assert any(c["test"].endswith("test_run") and c["base"] == "passed" and c["change"] == "failed"
               for c in diff["changed"])
    assert rv.git(graph_repo, "status", "--porcelain") == before       # snapshots left no trace
    assert not list(work.glob("snapshot-*"))


def test_diff_tests_no_distinction_and_inconclusive(graph_repo: Path, tmp_path: Path) -> None:
    calc = graph_repo / "src/risk/calc.py"
    calc.write_text(calc.read_text() + "\n# comment only\n")
    s = rv.resolve_scope(graph_repo, None, False)
    g = cm.build_graph(graph_repo)
    assert rv.run_diff_tests(s, g, tmp_path / "a")["verdict"] == "no test distinguishes the change"
    prof = graph_repo / ".claude/review/profile.md"
    prof.write_text(prof.read_text().replace(
        'gate_env_failures:', 'diff_test: "{python} -c \\"print(\'DATA_MISSING\'); raise SystemExit(2)\\""\n'
        'gate_env_failures:', 1))
    assert rv.run_diff_tests(s, g, tmp_path / "b")["verdict"] == "inconclusive"


def test_snapshot_links_are_removed_without_touching_target(graph_repo: Path, tmp_path: Path) -> None:
    (graph_repo / "datadir").mkdir()
    (graph_repo / "datadir" / "x.csv").write_text("1\n")
    dest = tmp_path / "snap"
    links = rv.snapshot(graph_repo, "HEAD", dest, ["datadir", "missing"])
    assert (dest / "src/core/engine.py").exists() and (dest / "datadir" / "x.csv").read_text() == "1\n"
    assert len(links) == 1                                              # missing dir is skipped
    rv.remove_snapshot(dest, links)
    assert not dest.exists() and (graph_repo / "datadir" / "x.csv").read_text() == "1\n"


def test_deep_report_ledger_lines(graph_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rv.dump_json(cm.build_map(graph_repo), cm.map_path(graph_repo))
    eng = graph_repo / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 3"))
    work = tmp_path / "w"
    _prep(graph_repo, work, "--effort", "deep")
    (work / "proofs").mkdir()
    (work / "proofs" / "test_core_scale.py").write_text("def test_x():\n    assert False\n")
    (tmp_path / "f.json").write_text(json.dumps({"findings": [], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(tmp_path / "f.json"), "--out-dir", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert "Effort: qualified" in out and "Impact reviewers: " in out and "src/api" in out
    assert "Differential tests (base" in out and "behaviour change observed" in out
    assert "test_core_scale.py" in out and "consider `deep`" not in out


def test_refuter_kept_does_not_override_later_upgrades() -> None:
    f = _finding(evidence="PROVEN")                                   # upgraded after the refuter ran
    out, stats = rv.apply_refuter([f], [{"id": f["id"], "verdict": "kept", "severity": "P1", "evidence": "TRACED"}])
    assert out[0]["evidence"] == "PROVEN" and out[0]["refuter"] == "kept" and stats["kept"] == 1
    out2, _ = rv.apply_refuter([f], [{"id": f["id"], "verdict": "downgraded", "severity": "P2", "evidence": "TRACED"}])
    assert (out2[0]["severity"], out2[0]["evidence"]) == ("P2", "TRACED")
    t = _finding()                                                    # TRACED, CROSS
    out3, _ = rv.apply_refuter([t], [{"id": t["id"], "verdict": "kept", "evidence": "INFERRED", "blast": "MODULE"}])
    assert (out3[0]["evidence"], out3[0]["blast"]) == ("INFERRED", "MODULE")         # kept may weaken
    out4, _ = rv.apply_refuter([t], [{"id": t["id"], "verdict": "kept", "evidence": "PROVEN", "blast": "CONTRACT"}])
    assert (out4[0]["evidence"], out4[0]["blast"]) == ("TRACED", "CROSS")            # never raise


def test_merge_collapses_same_defect_across_reviewers() -> None:
    a = _finding(id="numerics:pkg/core.py:price:slug-one", lens="numerics", evidence="TRACED")
    b = _finding(id="change-integrity:pkg/core.py:price:slug-two", evidence="PROVEN")
    c = _finding(id="numerics:pkg/core.py:price:other", severity="P2")  # different severity: separate
    merged = rv.merge([{"findings": [a]}, {"findings": [b, c]}], [])
    p1 = next(m for m in merged if m["severity"] == "P1")
    assert len(merged) == 2 and p1["evidence"] == "PROVEN" and p1["raised_by"] == 2
    assert p1["lens"] == "change-integrity+numerics"


def test_merge_keeps_distinct_defects_apart() -> None:
    one = _finding(id="l:pkg/core.py:price:one")
    two = _finding(id="l:pkg/core.py:price:two")
    assert len(rv.merge([{"findings": [one, two]}], [])) == 2                        # one reviewer, two ids
    bare = [_finding(id=f"l:pkg/core.py::{s}", symbol="") for s in ("a", "b")]
    assert len(rv.merge([{"findings": [bare[0]]}, {"findings": [bare[1]]}], [])) == 2  # no symbol: no match
    far = _finding(id="m:pkg/core.py:price:far", line=1 + rv.MERGE_LINES + 1)
    assert len(rv.merge([{"findings": [one]}, {"findings": [far]}], [])) == 2         # same symbol, far apart


# ------------------------------------------------------------------ review quality: locations + brevity
def test_fix_locations_relocates_wrong_lines(graph_repo: Path) -> None:
    eng = graph_repo / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 3"))
    scope = rv.resolve_scope(graph_repo, None, False)
    changed = next(ln for ln in sorted(rv._changed_lines(scope)["src/core/engine.py"]))
    bad = _finding(file="src/core/engine.py", symbol="_core", line=349)             # diff-relative number
    ok = _finding(id="x:src/core/engine.py:_core:ok", file="src/core/engine.py", symbol="_core", line=changed)
    method = _finding(id="x:src/core/engine.py:run:m", file="src/core/engine.py", symbol="run", line=1)
    ghost = _finding(id="x:nope.py:f:g", file="nope.py", symbol="f", line=3)
    warn: list[str] = []
    rv.fix_locations([bad, ok, method, ghost], scope, warn)
    assert bad["line"] == changed and bad["line_reported"] == 349                   # first changed line in _core
    assert ok["line"] == changed and "line_reported" not in ok                       # valid: untouched
    run_span = next((s, e) for q, s, e in rv._def_spans(eng) if q == "Engine.run")
    assert run_span[0] <= method["line"] <= run_span[1]                              # unchanged method: def start
    assert any("corrected to" in w for w in warn) and any("does not exist" in w for w in warn)


def test_clip_and_dedupe() -> None:
    assert rv._clip("One. Two. Three.") == "One. Two. …" and rv._clip("Only one.") == "Only one."
    assert rv._clip("A b. C d.", 1) == "A b. …"
    cmd = 'python -c "... _var_es(normal(1000), 0.975)" printed (1.8267, -0.1060)'
    assert rv._clip_chars(cmd) == cmd and rv._clip_chars("x " * 200).endswith(" …")


def test_merge_prefers_uncorrected_citation_on_evidence_tie() -> None:
    wrong = {**_finding(id="a:pkg/core.py:price:s1"), "line": 3, "line_reported": 349}
    right = {**_finding(id="b:pkg/core.py:price:s2"), "line": 2}
    assert rv.merge([{"findings": [wrong]}, {"findings": [right]}], [])[0]["line"] == 2
    items = ["data/ absent", "Data/ absent!", "golden PVs — data/ absent", "x", "y", "z", "w", "v"]
    out = rv._dedupe(items, cap=5)
    assert out[0] == "data/ absent" and "Data/ absent!" not in out and out[-1] == "+2 more in JSON"


def test_test_coverage_direct_indirect_none(graph_repo: Path) -> None:
    mods = cm.build_map(graph_repo)["modules"]
    cov = {v["path"]: v["metrics"].get("test_coverage") for v in mods.values() if v["metrics"]}
    assert cov["src/core/engine.py"] == "direct"                       # tests/test_engine.py imports it
    assert cov["src/common/storage.py"] == "indirect"                  # reached via engine
    assert cov["src/risk/calc.py"] == "indirect"                       # reached via the tests/helpers.py helper
    assert cov["src/feed/ingest.py"] == "none"                         # nothing imports it
    assert mods["src.common.storage"]["metrics"]["tests_reach"] >= 2   # test_engine + test_calc (via calc->engine)
    s = cm.summary(cm.build_map(graph_repo))
    assert "src/feed/ingest.py" in s["trunk_untested"]
    assert set(s["trunk_test_coverage"]) <= {"direct", "indirect", "none"}


def test_src_layout_imports_resolve_and_claude_dir_skipped(tmp_path: Path) -> None:
    r = tmp_path / "srclayout"
    files = {"src/pkg/__init__.py": "", "src/pkg/a.py": "def f():\n    return 1\n",
             "src/pkg/b.py": "from pkg.a import f\n\n\ndef g():\n    return f()\n",
             "test/test_b.py": "from pkg.b import g\n\n\ndef test_g():\n    assert g() == 1\n",
             ".claude/hooks/guard.py": "import os\n"}
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    g = cm.build_graph(r)
    assert "src.pkg.a" in g.imports["src.pkg.b"]                       # `from pkg.a` resolved via src/
    assert "src.pkg.b" in g.imports["test.test_b"]
    assert not any(p.startswith(".claude/") for p in g.paths.values())
    mods = cm.build_map(r)["modules"]
    assert mods["src.pkg.b"]["metrics"]["test_coverage"] == "direct"
    assert mods["src.pkg.a"]["metrics"]["test_coverage"] == "indirect"


# ------------------------------------------------------------------ PR / MR targets
def test_parse_pr_urls_numbers_and_rejects() -> None:
    assert rv.parse_pr("https://github.com/acme/widgets/pull/12") == ("github", "github.com",
                                                                               "acme/widgets", 12)
    assert rv.parse_pr("https://gitlab.example.com/grp/sub/repo/-/merge_requests/7/") == (
        "gitlab", "gitlab.example.com", "grp/sub/repo", 7)
    assert rv.parse_pr("42") == (None, None, None, 42)
    for bad in ("https://github.com/a/b/issues/3", "http://github.com/a/b/pull/1", "1; rm -rf /",
                "https://github.com/a/b/pull/1 && x"):
        with pytest.raises(SystemExit):
            rv.parse_pr(bad)


@pytest.fixture
def pr_setup(graph_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' holding main + refs/pull/7/head (a branch changing engine._core); a clone
    of it with the pack present only in the working copy (as during the pack rollout)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(graph_repo), str(origin)], check=True)
    work = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    eng = work / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 3"))
    _git(work, "commit", "-qam", "scale core by 3")
    _git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    clone = tmp_path / "reviewer"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    pack = clone / ".claude" / "review"
    if not (pack / "profile.md").exists():                             # pack ignored/untracked: copy it in
        import shutil
        shutil.copytree(graph_repo / ".claude" / "review", pack)
    return origin, clone


def test_pr_review_uses_temp_worktree_and_cleans_up(pr_setup: tuple[Path, Path], tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    _, clone = pr_setup
    def state() -> tuple[str, list[str]]:                              # fixture repo has no out/ ignore rule
        status = [ln for ln in rv.git(clone, "status", "--porcelain").splitlines() if ".claude/review/out" not in ln]
        return rv.git(clone, "rev-parse", "--abbrev-ref", "HEAD"), status
    before = state()
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(clone), "--pr", "7", "--intent", "i", "--work", str(work), "--no-gate"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["target"].startswith("PR 7") and summary["commits"] == ["scale core by 3"]
    assert [f["path"] for f in json.loads((work / "scope.json").read_text())["files"]] == ["src/core/engine.py"]
    wt = Path(summary["review_repo"])
    assert wt.exists() and "x * 3" in (wt / "src/core/engine.py").read_text()      # PR code, not the clone's
    assert "x * 2" in (clone / "src/core/engine.py").read_text()                    # user's checkout untouched
    assert summary["reach"] == "CONTRACT"
    (tmp_path / "f.json").write_text(json.dumps({"findings": [], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(tmp_path / "f.json")])
    assert "PR 7" in capsys.readouterr().out
    assert not wt.exists() and len(rv.git(clone, "worktree", "list").splitlines()) == 1
    assert (clone / ".claude/review/out/latest.json").exists()                      # report outlives the worktree
    rv.main(["report", "--work", str(work), "--findings", str(tmp_path / "f.json")])  # re-run: worktree gone
    assert "PR 7" in capsys.readouterr().out
    assert state() == before


def test_pr_unknown_number_and_wrong_repo_fail_cleanly(pr_setup: tuple[Path, Path], tmp_path: Path) -> None:
    _, clone = pr_setup
    with pytest.raises(SystemExit, match="could not fetch"):
        rv.main(["prep", "--repo", str(clone), "--pr", "99", "--intent", "i", "--work", str(tmp_path / "w"),
                 "--no-gate"])
    rv.git(clone, "remote", "set-url", "origin", "https://github.com/someone/else.git")
    with pytest.raises(SystemExit, match=r"origin is github.com/someone/else"):
        rv.fetch_pr(clone, "https://github.com/acme/widgets/pull/7", None)


def test_merged_pr_is_reviewed_against_pre_merge_base(pr_setup: tuple[Path, Path], tmp_path: Path) -> None:
    _, clone = pr_setup
    _git(clone, "fetch", "-q", "origin", "refs/pull/7/head")
    _git(clone, "merge", "-q", "--no-ff", "-m", "Merge PR 7", "FETCH_HEAD")        # merge into main, push it
    _git(clone, "push", "-q", "origin", "HEAD:main")
    _git(clone, "reset", "-q", "--hard", "HEAD~1")                                  # reviewer's main is older
    pr = rv.fetch_pr(clone, "7", None)
    assert pr["merged"] and "pre-merge base" in pr["merged"]
    scope = rv.resolve_scope(clone, f"{pr['merge_base']}..{pr['head']}", False)
    assert [f["path"] for f in scope["files"]] == ["src/core/engine.py"]            # not an empty scope


# ------------------------------------------------------------------ posting to GitHub (fake API)
class FakeGitHub:
    """In-memory PR: issue comments, review comments, threads, labels, title."""

    def __init__(self, head: str) -> None:
        self.head, self.title = head, "Scale core"
        self.issue_comments: list[dict] = []
        self.review_comments: list[dict] = []
        self.labels: set[str] = set()
        self.repo_labels: set[str] = set()
        self.resolved: list[str] = []
        self.calls: list[tuple] = []

    def api(self, method: str, path: str, payload: dict | None = None):
        self.calls.append((method, path))
        from urllib.parse import unquote
        if method == "GET" and path.endswith("/pulls/7"):
            return {"head": {"sha": self.head}, "title": self.title, "labels": [{"name": n} for n in self.labels]}
        if path.endswith("/issues/7/comments"):
            if method == "GET":
                return list(self.issue_comments)
            self.issue_comments.append({"id": len(self.issue_comments) + 1, "body": payload["body"]})
            return {}
        if "/issues/comments/" in path:                                   # PATCH summary
            cid = int(path.rsplit("/", 1)[1])
            next(c for c in self.issue_comments if c["id"] == cid)["body"] = payload["body"]
            return {}
        if method == "GET" and path.endswith("/pulls/7/comments"):
            return list(self.review_comments)
        if path.endswith("/pulls/7/reviews"):
            assert payload["event"] == "COMMENT"                          # never approve / request changes
            for c in payload["comments"]:
                self.review_comments.append({"id": 100 + len(self.review_comments), "body": c["body"],
                                             "in_reply_to_id": None, "line": c["line"]})
            return {}
        if path.endswith("/replies"):
            rid = int(path.split("/comments/")[1].split("/")[0])
            self.review_comments.append({"id": 100 + len(self.review_comments), "body": payload["body"],
                                         "in_reply_to_id": rid})
            return {}
        if path.endswith("/labels") and "/issues/" not in path:          # create repo label
            if payload["name"] in self.repo_labels:
                raise rv.PostError("HTTP 422 Validation Failed already_exists")
            self.repo_labels.add(payload["name"])
            return {}
        if method == "DELETE" and "/issues/7/labels/" in path:
            self.labels.discard(unquote(path.rsplit("/", 1)[1]))
            return {}
        if method == "POST" and path.endswith("/issues/7/labels"):
            self.labels |= set(payload["labels"])
            return {}
        if method == "PATCH" and path.endswith("/pulls/7"):
            self.title = payload["title"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    def graphql(self, query: str, variables: dict):
        if "reviewThreads" in query:
            nodes = [{"id": f"T{c['id']}", "isResolved": f"T{c['id']}" in self.resolved,
                      "comments": {"nodes": [{"body": c["body"]}]}}
                     for c in self.review_comments if not c["in_reply_to_id"]]
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
        if "unresolveReviewThread" in query:
            self.resolved.remove(variables["id"])
            return {}
        self.resolved.append(variables["id"])
        return {}


def _post_fixture(graph_repo: Path):
    _git(graph_repo, "remote", "add", "origin", "https://github.com/me/proj.git")
    eng = graph_repo / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 3"))
    scope = rv.resolve_scope(graph_repo, None, False)
    blast = rv.blast_map(scope)
    changed_line = min(rv._changed_lines(scope)["src/core/engine.py"])
    return scope, blast, changed_line


def _report(scope: dict, verdict: str, findings: list[dict]) -> dict:
    return {"verdict": verdict, "effort": "standard", "head_sha": scope["head_sha"], "intent": "scale core",
            "findings": findings, "report_path": "C:/local/out/x.json",
            "ledger": {"lenses": ["change-integrity"], "reviewers": 1, "gate": [], "skipped": [], "checked": [],
                       "refuter": {"ran": False}, "suppressed": 0, "tokens": {"reviewer": 50000, "total": 50000},
                       "wall_s": 125}}


def test_post_creates_then_updates_idempotently(graph_repo: Path) -> None:
    scope, blast, line = _post_fixture(graph_repo)
    fake = FakeGitHub(scope["head_sha"])
    p1 = _finding(id="change-integrity:src/core/engine.py:_core:scale", file="src/core/engine.py", line=line,
                  symbol="_core")
    p0_off = _finding(id="numerics:src/core/engine.py:Engine:x", severity="P0", file="src/core/engine.py",
                      line=1, symbol="Engine")
    r1 = _report(scope, "BLOCK", [p0_off, p1, _finding(id="a:b:c:p2", severity="P2")])
    acts = rv.post_review(r1, scope, blast, pr_number=7, title_prefix=True, poster=fake)
    summary = fake.issue_comments[0]["body"]
    assert len(fake.issue_comments) == 1 and rv.SUMMARY_MARK in summary and "BLOCK**" in summary
    assert "C:/local" not in summary and "Cost: 50k tokens (1 agent)" in summary
    assert "Not on a diff line" in summary and "`src/core/engine.py:1` (P0)" in summary
    assert [c["line"] for c in fake.review_comments] == [line]                      # only the on-diff P1
    assert {"ai-verdict:block", "review:contract"} <= fake.labels
    assert fake.title == "[CONTRACT][BLOCK] Scale core" and any("inline" in a for a in acts)

    r2 = _report(scope, "SHIP-WITH-NOTES", [_finding(id="a:b:c:p2", severity="P2")])  # P0/P1 fixed
    rv.post_review(r2, scope, blast, pr_number=7, title_prefix=True, poster=fake)
    assert len(fake.issue_comments) == 1 and "SHIP-WITH-NOTES**" in fake.issue_comments[0]["body"]
    assert fake.resolved == ["T100"]                                                 # P1 thread resolved
    assert any(c["in_reply_to_id"] == 100 and "no longer reported" in c["body"] for c in fake.review_comments)
    assert "ai-verdict:block" not in fake.labels and "ai-verdict:ship-with-notes" in fake.labels
    assert fake.title == "[CONTRACT][SHIP-WITH-NOTES] Scale core"                    # replaced, not stacked
    replies = len(fake.review_comments)
    acts3 = rv.post_review(r2, scope, blast, pr_number=7, poster=fake)               # third run: no duplicates
    assert len([c for c in fake.review_comments if not c["in_reply_to_id"]]) == 1
    assert len(fake.review_comments) == replies and not any("resolved" in a for a in acts3)  # no repeat reply
    assert fake.resolved == ["T100"]

    r4 = _report(scope, "FIX-FIRST", [p1])                                           # the P1 comes back
    acts4 = rv.post_review(r4, scope, blast, pr_number=7, poster=fake)
    assert fake.resolved == [] and any("reopened" in a for a in acts4)
    assert any(c["in_reply_to_id"] == 100 and "reported again" in c["body"] for c in fake.review_comments)
    assert len([c for c in fake.review_comments if not c["in_reply_to_id"]]) == 1    # same thread, not a new one
    rv.post_review(_report(scope, "SHIP-WITH-NOTES", []), scope, blast, pr_number=7, poster=fake)
    assert fake.resolved == ["T100"]                                                 # gone again: resolved again


def test_post_says_so_when_token_cannot_resolve_threads(graph_repo: Path) -> None:
    scope, blast, line = _post_fixture(graph_repo)

    class ReadOnlyToken(FakeGitHub):                                  # Contents: read-only PAT
        def graphql(self, query: str, variables: dict):
            if "resolveReviewThread" in query:
                raise rv.PostError("FORBIDDEN: Resource not accessible by personal access token")
            return super().graphql(query, variables)
    fake = ReadOnlyToken(scope["head_sha"])
    p1 = _finding(id="change-integrity:src/core/engine.py:_core:scale", file="src/core/engine.py", line=line,
                  symbol="_core")
    rv.post_review(_report(scope, "FIX-FIRST", [p1]), scope, blast, pr_number=7, poster=fake)
    acts = rv.post_review(_report(scope, "SHIP", []), scope, blast, pr_number=7, poster=fake)
    note = next(a for a in acts if "no longer reported" in a)
    assert "1 thread(s) not resolved" in note and "resolve them on GitHub" in note
    assert "thread(s) resolved" not in note and fake.resolved == []
    assert any(c["in_reply_to_id"] == 100 for c in fake.review_comments)            # the reply still went out


def test_post_reports_other_thread_errors_as_they_are(graph_repo: Path) -> None:
    scope, blast, line = _post_fixture(graph_repo)

    class FlakyNetwork(FakeGitHub):                                   # not a permission problem
        def graphql(self, query: str, variables: dict):
            if "resolveReviewThread" in query:
                raise rv.PostError("gh api POST graphql: connection reset by peer")
            return super().graphql(query, variables)
    fake = FlakyNetwork(scope["head_sha"])
    p1 = _finding(id="change-integrity:src/core/engine.py:_core:scale", file="src/core/engine.py", line=line,
                  symbol="_core")
    rv.post_review(_report(scope, "FIX-FIRST", [p1]), scope, blast, pr_number=7, poster=fake)
    acts = rv.post_review(_report(scope, "SHIP", []), scope, blast, pr_number=7, poster=fake)
    assert not any("lacks repo write access" in a for a in acts)                    # not blamed on the token
    assert any(a.startswith("could not resolve thread for") and "connection reset" in a for a in acts)
    assert any(a.startswith("1 earlier inline finding(s) no longer reported: replied") for a in acts)


def test_post_refuses_stale_head_and_gitlab(graph_repo: Path) -> None:
    scope, blast, _ = _post_fixture(graph_repo)
    fake = FakeGitHub("f" * 40)
    with pytest.raises(rv.PostError, match="re-run the review"):
        rv.post_review(_report(scope, "SHIP", []), scope, blast, pr_number=7, poster=fake)
    rv.post_review(_report(scope, "SHIP", []), scope, blast, pr_number=7, poster=fake, allow_stale=True)
    with pytest.raises(rv.PostError, match="GitLab"):
        rv.post_review(_report(scope, "SHIP", []), {**scope, "pr": {"kind": "gitlab", "number": 7}}, blast,
                       poster=fake)


def test_github_poster_keeps_payload_off_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    class Proc:
        returncode, stdout, stderr = 0, "{}", ""

    def fake_run(args, **kw):
        seen["args"], seen["input"] = args, kw.get("input")
        return Proc()
    monkeypatch.setattr(rv, "find_gh", lambda: "gh")
    monkeypatch.setattr(rv.subprocess, "run", fake_run)
    p = rv.GitHubPoster(tmp_path)
    p.api("POST", "repos/me/proj/issues/7/comments", {"body": "evil $(rm -rf /) `x` body"})
    assert "evil" not in " ".join(seen["args"]) and "evil" in seen["input"] and "--input" in seen["args"]
    dry = rv.GitHubPoster(tmp_path, dry_run=True)
    seen.clear()
    dry.api("POST", "repos/me/proj/issues/7/comments", {"body": "x"})
    assert not seen and dry.sent == [("POST", "repos/me/proj/issues/7/comments")]   # dry run sends nothing


def test_cost_parsing_and_line() -> None:
    t = rv._parse_cost(["reviewer=50163,refuter=38328", "impact-1=52302"])
    assert t["total"] == 140793
    assert rv._cost_line({"tokens": t, "wall_s": 252}) == "Cost: 141k tokens (3 agents) · 4m 12s review time"
    assert rv._cost_line({"tokens": {}, "wall_s": None}) is None


# ------------------------------------------------------------------ robustness (self-review fixes)
def test_prep_survives_deleted_tracked_file_and_nested_repo(repo: Path, tmp_path: Path) -> None:
    (repo / "pkg" / "api.py").unlink()                                 # tracked, deleted, not staged
    nested = repo / "vendor"
    nested.mkdir()
    (nested / "x.py").write_text("x = 1\n")
    _git(nested, "init", "-q")                                         # listed by ls-files as `vendor/`
    scope = rv.resolve_scope(repo, None, False)
    assert not any(f["path"].startswith("vendor") for f in scope["files"])
    rv.main(["prep", "--repo", str(repo), "--intent", "i", "--work", str(tmp_path / "w"), "--no-gate"])
    assert json.loads((tmp_path / "w" / "scope.json").read_text())["line_map"]["pkg/core.py"]


def test_diff_paths_survive_noprefix_and_quoting(repo: Path) -> None:
    assert rv.diff_new_path("+++ b/pkg/core.py") == "pkg/core.py"
    assert rv.diff_new_path("+++ /dev/null") is None
    assert rv.diff_new_path("+++ b/my file.py\t") == "my file.py"
    assert rv.diff_new_path('+++ "b/caf\\303\\251 \\"q\\".py"') == 'café "q".py'
    _git(repo, "config", "diff.noprefix", "true")
    scope = rv.resolve_scope(repo, None, False)
    assert rv._changed_lines(scope)["pkg/core.py"] == {1, 2, 6}
    assert "price" in rv.changed_symbols(repo, scope)["pkg/core.py"]


def test_shell_paths_drops_unsafe_names() -> None:
    dropped: list[str] = []
    assert rv.shell_paths(["tests/test_a.py", "a & calc.py", "b$(x).py", "c d.py"], dropped) == "tests/test_a.py"
    assert dropped == ["a & calc.py", "b$(x).py", "c d.py"]


def test_pr_prep_does_not_run_pr_code_by_default(pr_setup: tuple[Path, Path], tmp_path: Path) -> None:
    _, clone = pr_setup
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(clone), "--pr", "7", "--intent", "i", "--work", str(work), "--effort", "deep",
             "--force-deep"])
    gate = json.loads((work / "gate.json").read_text())
    assert gate["results"] == [] and "--run-pr-code" in gate["note"]
    assert "--run-pr-code" in json.loads((work / "diff_tests.json").read_text())["why"]
    rv.main(["report", "--work", str(work), "--findings", str(_empty_findings(tmp_path))])


def _empty_findings(tmp_path: Path) -> Path:
    f = tmp_path / "empty.json"
    f.write_text(json.dumps({"findings": [], "checked": [], "not_checked": []}))
    return f


def test_pr_review_never_trusts_the_prs_own_pack(pr_setup: tuple[Path, Path], tmp_path: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    origin, clone = pr_setup
    author = tmp_path / "attacker"
    subprocess.run(["git", "clone", "-q", str(origin), str(author)], check=True)
    pack = author / ".claude" / "review"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "profile.md").write_text('---\ngate: ["echo pwned"]\n---\n')
    (pack / "suppressions.md").write_text("- change-integrity:src/core/engine.py:_core:x — wontfix — mine — 2026-10-02\n")
    eng = author / "src/core/engine.py"
    eng.write_text(eng.read_text().replace("x * 2", "x * 4"))
    _git(author, "add", "-Af")
    _git(author, "commit", "-qm", "scale core, suppress my finding")
    _git(author, "push", "-q", "origin", "HEAD:refs/pull/8/head")

    rv.main(["prep", "--repo", str(clone), "--pr", "8", "--intent", "i", "--work", str(tmp_path / "w"), "--no-gate"])
    wt = Path(json.loads(capsys.readouterr().out)["review_repo"])
    wt_pack = wt / ".claude" / "review"
    assert (wt_pack / "profile.md").read_text() == (clone / ".claude/review/profile.md").read_text()
    assert "pwned" not in (wt_pack / "profile.md").read_text()
    assert "change-integrity:src/core/engine.py:_core:x" not in rv.load_suppressed_ids(wt)

    import shutil
    shutil.rmtree(clone / ".claude" / "review")                       # no pack of your own: the PR's is dropped
    rv.main(["prep", "--repo", str(clone), "--pr", "8", "--intent", "i", "--work", str(tmp_path / "w2"), "--no-gate"])
    wt2 = Path(json.loads(capsys.readouterr().out)["review_repo"])
    assert not (wt2 / ".claude" / "review").exists()


def test_refuter_can_only_cut_and_survives_unknown_verdicts() -> None:
    f = _finding(severity="P2", evidence="TRACED", blast="MODULE")
    warn: list[str] = []
    out, stats = rv.apply_refuter([f], [{"id": f["id"], "verdict": "downgraded", "severity": "P0",
                                         "evidence": "PROVEN", "blast": "CONTRACT"}], warn)
    assert (out[0]["severity"], out[0]["evidence"], out[0]["blast"]) == ("P2", "TRACED", "MODULE")
    assert out[0]["refuter"] == "kept" and stats["kept"] == 1 and "lowered nothing" in warn[0]
    out, stats = rv.apply_refuter([f], [{"id": f["id"], "verdict": "refuted"}], warn)
    assert out[0]["refuter"] == "unrefuted" and stats["in"] == 0 and "unknown refuter verdict" in warn[-1]
    out, _ = rv.apply_refuter([f], [{"id": f["id"], "verdict": "downgraded", "severity": "P3", "blast": "CONTRACT"}])
    assert (out[0]["severity"], out[0]["blast"]) == ("P3", "MODULE")                 # lowered; raise ignored
    _, stats = rv.apply_refuter([f], [{"id": f["id"], "verdict": "dropped"}])
    assert stats["dropped"] == 1


def _status_report(repo: Path, findings: list[dict], sha: str | None = None) -> dict:
    for f in findings:                                                # as `report` stamps them
        if fp := rv.code_fingerprint(repo / f["file"], f):
            f["code_fp"] = fp
    return {"target": "t", "head_sha": sha or rv.git(repo, "rev-parse", "HEAD").strip(), "verdict": "FIX-FIRST",
            "findings": findings}


def test_status_on_a_working_tree_review(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # the fixture's change is uncommitted: the review covers code that HEAD doesn't have
    report = _status_report(repo, [
        _finding(id="a:pkg/core.py:price:x", file="pkg/core.py", symbol="price", line=1),
        _finding(id="b:pkg/core.py:_helper:y", file="pkg/core.py", symbol="_helper", line=5, severity="P2"),
        _finding(id="domain:pkg/api.py:handler:old-thing", file="pkg/api.py", symbol="handler", line=4,
                 severity="P2", summary="Pipe | in text."),
        {**_finding(id="c:pkg/extra.py:new_fn:z", file="pkg/extra.py", symbol="new_fn", line=1, severity="P3")}])
    del report["findings"][3]["code_fp"]                              # an older report: no fingerprint
    rows = rv.finding_status(repo, report, rv.load_suppressions(repo))
    assert [r["status"] for r in rows] == ["open", "open", "suppressed", "unknown"]   # right after the review
    core = repo / "pkg" / "core.py"
    core.write_text(core.read_text().replace("return x * scale", "return x * scale * 1"))  # edit price only
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix price")                          # committing doesn't change the answer
    rows = rv.finding_status(repo, report, rv.load_suppressions(repo))
    assert [r["status"] for r in rows] == ["changed", "open", "suppressed", "unknown"]
    assert rows[2]["note"].startswith("wontfix")
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report))
    rv.main(["status", "--repo", str(repo), "--report", str(path)])
    out = capsys.readouterr().out
    assert "1 open · 1 changed since review (recheck) · 1 suppressed · 1 unknown" in out
    assert "| 1 | P1 |" in out and "| 2 | P2 |" in out and "| 3 |" not in out          # suppressed hidden
    rv.main(["status", "--repo", str(repo), "--report", str(path), "--all"])
    assert r"Pipe \| in text." in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no review report"):
        rv.main(["status", "--repo", str(tmp_path / "w")])


def test_status_is_unknown_when_checkout_lacks_the_reviewed_commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "pr")
    core = repo / "pkg" / "core.py"
    core.write_text(core.read_text().replace("return x * scale", "return x * scale + 0"))
    _git(repo, "commit", "-qam", "pr change")
    report = _status_report(repo, [_finding(file="pkg/core.py", symbol="price", line=1)])  # reviewed on the PR
    _git(repo, "checkout", "-q", "main")                              # back on the base branch
    rows = rv.finding_status(repo, report, {})
    assert rows[0]["status"] == "unknown" and "doesn't contain the reviewed commit" in rows[0]["note"]


def test_pr_report_rerun_keeps_suppressions(pr_setup: tuple[Path, Path], tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    _, clone = pr_setup
    fid = "change-integrity:src/core/engine.py:_core:scale"
    with (clone / ".claude/review/suppressions.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {fid} — accepted-risk — known — 2026-10-02\n")
    work = tmp_path / "w"
    rv.main(["prep", "--repo", str(clone), "--pr", "7", "--intent", "i", "--work", str(work), "--no-gate"])
    capsys.readouterr()
    f = tmp_path / "f.json"
    f.write_text(json.dumps({"findings": [_finding(id=fid, file="src/core/engine.py", symbol="_core", line=1)],
                             "checked": [], "not_checked": []}))
    for _ in range(2):                                                # second run: the worktree is gone
        rv.main(["report", "--work", str(work), "--findings", str(f), "--out-dir", str(tmp_path / "o")])
        latest = json.loads((tmp_path / "o" / "latest.json").read_text())
        assert latest["findings"] == [] and latest["ledger"]["suppressed"] == 1
    other = _finding(id="x:src/core/engine.py:_core:other", file="src/core/engine.py", symbol="_core", line=1)
    f.write_text(json.dumps({"findings": [other], "checked": [], "not_checked": []}))
    rv.main(["report", "--work", str(work), "--findings", str(f), "--out-dir", str(tmp_path / "o")])
    assert json.loads((tmp_path / "o" / "latest.json").read_text())["findings"][0].get("code_fp")  # from the head
    capsys.readouterr()


def test_render_escapes_pipes_in_table_cells(repo: Path) -> None:
    scope = rv.resolve_scope(repo, None, False)
    f = _finding(summary="Splits on a | b without a guard.")
    report = {"findings": [f], "effort": "standard", "verdict": "FIX-FIRST", "intent": "i", "report_path": "r.json",
              "ledger": {"lenses": ["change-integrity"], "reviewers": 1, "gate": [], "skipped": [], "checked": [],
                         "refuter": {"ran": False}, "suppressed": 0, "tokens": {}, "wall_s": None}}
    row = next(ln for ln in rv.render(report, scope, rv.blast_map(scope)).splitlines()
               if ln.startswith("| 1 |"))
    assert r"a \| b" in row and row.count(" | ") == 5                  # still exactly six cells


def test_codemap_not_stale_just_because_of_untracked_files(graph_repo: Path) -> None:
    import codemap as cm
    tracked = cm.build_graph(graph_repo)
    (graph_repo / "src" / "core" / "newmod.py").write_text("from src.core.engine import Engine\n")
    with_new = cm.build_graph(graph_repo, ["src/core/newmod.py"])
    assert with_new.edge_hash() != tracked.edge_hash()                         # the new file adds an edge
    assert with_new.edge_hash(tracked_only=True) == tracked.edge_hash()        # but the map isn't stale

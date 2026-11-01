"""Shared setup: a scratch project with a bare origin, so origin/main, merges and branches are real.

Events go to a per-test folder. The project is a tiny pricing package; change `c1` adds
`zero_price(face, rate, years) -> float` (closed form face * exp(-rate * years)).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

PROFILE = """\
spec_root = "tests/spec"
unit_root = "tests/unit"
src_roots = ["src"]
pythonpath = ["src"]
plugins = []
budget = 3
"""

BONDS = '''\
"""Bond maths."""
import math


def discount_factor(rate: float, years: float) -> float:
    return math.exp(-rate * years)
'''

SPEC = """\
# c1: zero-coupon bond price

## Requirement
Price a zero-coupon bond.

## Acceptance criteria
- **C1**: price = face * exp(-rate * years), relative tolerance 1e-12.

## Interface
- `pricing.bonds.zero_price(face: float, rate: float, years: float) -> float`
"""

STUB = '''

def zero_price(face: float, rate: float, years: float) -> float:
    """Price of a zero-coupon bond."""
    raise NotImplementedError("tdd:c1")
'''

IMPL = '''

def zero_price(face: float, rate: float, years: float) -> float:
    """Price of a zero-coupon bond."""
    return face * discount_factor(rate, years)
'''

TESTS = '''\
import math

import pytest

from pricing.bonds import zero_price


@pytest.mark.criterion("C1")
def test_known_value():
    # oracle: closed form 100 * exp(-0.05 * 2)
    assert zero_price(100.0, 0.05, 2.0) == pytest.approx(100 * math.exp(-0.1), rel=1e-12)


@pytest.mark.criterion("C1")
@pytest.mark.metamorphic
def test_linear_in_face():
    # oracle: price is linear in face value and positive
    for f in (1.0, 50.0, 250.0):
        p = zero_price(f, 0.03, 5.0)
        assert p > 0
        assert zero_price(2 * f, 0.03, 5.0) == pytest.approx(2 * p, rel=1e-12)
'''


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout


def write(repo: Path, rel: str, text: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def commit_all(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def merge_to_main(repo: Path, branch: str, n: int = 1, owner: str = "owner") -> None:
    """Merge like GitHub does: a merge commit whose subject names the head branch, then push."""
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from {owner}/{branch}",
        "-m", "PR title here")
    git(repo, "push", "-q", "origin", "main")


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDD_HISTORY_DIR", str(tmp_path / "tdd-history"))
    monkeypatch.setenv("TDD_PYTHON", sys.executable)
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A clone of a bare origin with the tdd pack on main and no changes yet."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "faultline"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    write(repo, ".claude/tdd/profile.toml", PROFILE)
    write(repo, ".claude/tdd/protected.txt", "tests/golden/**\n")
    write(repo, ".gitignore", ".claude/tdd/state/\n__pycache__/\n")
    write(repo, "src/pricing/__init__.py", "")
    write(repo, "src/pricing/bonds.py", BONDS)
    write(repo, "tests/golden/curve.json", '{"10y": 0.042}\n')
    write(repo, "tests/unit/test_df.py", "from pricing.bonds import discount_factor\n\n"
                                         "def test_df():\n    assert discount_factor(0.0, 1.0) == 1\n")
    write(repo, "tests/spec/conftest.py", "")
    commit_all(repo, "initial")
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@pytest.fixture
def spec_branch(project: Path) -> Path:
    """Branch spec/c1 with the spec, stub and tests written (not committed)."""
    git(project, "checkout", "-q", "-b", "spec/c1")
    write(project, "docs/specs/c1.md", SPEC)
    (project / "src/pricing/bonds.py").write_text(BONDS + STUB, encoding="utf-8")
    write(project, "tests/spec/c1/test_zero_price.py", TESTS)
    return project


@pytest.fixture
def locked(spec_branch: Path) -> Path:
    """c1 locked and merged to main; HEAD on a fresh impl/c1."""
    import tdd
    import tddlib as tl
    prof = tl.load_profile(spec_branch)
    assert prof is not None
    tdd.write_lock(spec_branch, tdd.build_lock(spec_branch, prof, "c1"))
    commit_all(spec_branch, "test(spec): c1")
    merge_to_main(spec_branch, "spec/c1", 1)
    git(spec_branch, "checkout", "-q", "-b", "impl/c1")
    return spec_branch


def implement(repo: Path) -> None:
    (repo / "src/pricing/bonds.py").write_text(BONDS + IMPL, encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

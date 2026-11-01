"""The public-safety check rejects real shapes and values, and passes placeholders.

Fixtures are assembled at runtime so this file does not trip the check itself.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_public_safe.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_public_safe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("D:" + "\\Tools\\AI\\x", "machine path"),
        ("D:" + "/Tools/AI/x", "machine path"),
        ("C:" + "\\\\Users\\\\me", "machine path"),
        ("/home" + "/someone/x", "home directory"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "access key"),
        ("-----BEGIN RSA " + "PRIVATE KEY-----", "private key"),
        ("me" + "@gmail.com", "personal email"),
        ("Co-Authored-" + "By: A <a@b.c>", "co-author trailer"),
        ("account 1234" + "56789012", "account id"),
    ],
)
def test_rejects_each_shape(line: str, kind: str) -> None:
    assert kind in check.scan_line(line, [])


@pytest.mark.parametrize(
    "line",
    ["<python> <config>/skills/x.py", "C:/Program Files/ClaudeCode", "arn:aws:iam::<acct>:role/x",
     "account 1111" + "22223333", "11111111-1111-1111-1111-111111111111", "user@example.com"],
)
def test_passes_placeholders(line: str) -> None:
    assert check.scan_line(line, []) == []


def test_denylist_is_case_insensitive_and_skips_license(tmp_path: Path) -> None:
    deny = tmp_path / "deny"
    deny.write_text("# a comment\nSecretName\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "doc.md").write_text("mentions secretNAME here\n", encoding="utf-8")
    (tree / "LICENSE").write_text("Copyright SecretName\n", encoding="utf-8")
    assert check.load_literals(deny) == ["secretname"]
    assert check.main([str(tree), "--denylist", str(deny)]) == 1
    (tree / "doc.md").write_text("clean\n", encoding="utf-8")
    assert check.main([str(tree), "--denylist", str(deny)]) == 0


def test_finding_never_contains_the_value(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    value = "me" + "@gmail.com"
    (tmp_path / "doc.md").write_text(value + "\n", encoding="utf-8")
    assert check.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "doc.md:1: personal email" in err and value not in err

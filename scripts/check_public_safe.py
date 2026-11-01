#!/usr/bin/env python
"""Fail if a file holds something that must not reach the public repo.

Usage: python scripts/check_public_safe.py [root] [--denylist FILE]

Scans the files git tracks under root (every file when root is not a repo). Works from patterns
alone on a clean clone. With a denylist, one literal per line matched case-insensitively, it also
rejects those values.

Findings report the file, line and kind, never the matched text, so a hit does not copy the value
into a CI log.

This catches known shapes and known values. It cannot catch something personal written in plain
prose, so it does not replace reading the diff before a sync.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    "machine path": re.compile(r"\b[A-Za-z]:[\\/]+(?:Tools|Users)\b"),
    "home directory": re.compile(r"/(?:Users|home)/[A-Za-z]\w+/"),
    "access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "personal email": re.compile(r"[\w.+-]+@(?:gmail|outlook|hotmail|yahoo|icloud)\.com", re.I),
    "co-author trailer": re.compile(r"^Co-Authored-By: .*<", re.I),
}
ACCOUNT_ID = re.compile(r"(?<![\d-])\d{12}(?![\d-])")
PLACEHOLDER_ACCOUNTS = {"000000000000", "111111111111", "111122223333"}
# The copyright line legitimately carries the author's name.
LITERAL_EXEMPT = {"LICENSE"}
BINARY_SUFFIXES = {".png", ".jpg", ".ico"}


def load_literals(denylist: Path | None) -> list[str]:
    """Return the lower-cased values to reject.

    Args:
        denylist: A file with one literal per line, or None.

    Returns:
        The literals; empty without a denylist.
    """
    if denylist is None:
        return []
    lines = denylist.read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def scan_line(line: str, literals: list[str]) -> list[str]:
    """Return the kinds of problem found on one line.

    Args:
        line: The line to check.
        literals: Lower-cased values to reject.

    Returns:
        The kind of each problem found; empty when the line is clean.
    """
    kinds = [kind for kind, pattern in PATTERNS.items() if pattern.search(line)]
    if set(ACCOUNT_ID.findall(line)) - PLACEHOLDER_ACCOUNTS:
        kinds.append("account id")
    lowered = line.lower()
    if any(value in lowered for value in literals):
        kinds.append("denylisted value")
    return kinds


def scan_file(path: Path, relative: str, literals: list[str]) -> list[str]:
    """Return one finding per problem in a file.

    Args:
        path: The file on disk.
        relative: Its path under the root, used in findings.
        literals: Lower-cased values to reject.

    Returns:
        Findings in `path:line: kind` form.
    """
    if path.suffix in BINARY_SUFFIXES:
        return []
    active = [] if relative in LITERAL_EXEMPT else literals
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        f"{relative}:{number}: {kind}"
        for number, line in enumerate(text.splitlines(), 1)
        for kind in scan_line(line, active)
    ]


def files_under(root: Path) -> list[str]:
    """Return the files to scan, relative to root and with forward slashes.

    Args:
        root: The directory to scan.

    Returns:
        Tracked files when root is a git repo, else every file.
    """
    run = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
    if run.returncode == 0 and (root / ".git").exists():
        return run.stdout.splitlines()
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


def main(argv: list[str] | None = None) -> int:
    """Scan the files under a root.

    Args:
        argv: Command-line arguments, or None for sys.argv.

    Returns:
        Process exit code: 0 when clean, 1 when anything was found.
    """
    parser = argparse.ArgumentParser(description="Reject machine paths, keys and listed values.")
    parser.add_argument("root", nargs="?", type=Path, default=REPO_ROOT)
    parser.add_argument("--denylist", type=Path)
    args = parser.parse_args(argv)
    literals = load_literals(args.denylist)
    findings = [
        finding
        for relative in files_under(args.root)
        if (args.root / relative).is_file()
        for finding in scan_file(args.root / relative, relative, literals)
    ]
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"check-public-safe: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

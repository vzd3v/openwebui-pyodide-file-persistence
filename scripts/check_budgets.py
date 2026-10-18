"""Enforce focused-test maintenance budgets, including an optional git diff."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def volume(folder: str) -> tuple[int, int]:
    paths = sorted((ROOT / folder).rglob("*.py"))
    return len(paths), sum(
        sum(bool(line.strip()) for line in path.read_text().splitlines()) for path in paths
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Commit used for test/source diff ratio")
    args = parser.parse_args()
    source_files, source_lines = volume("src")
    test_files, test_lines = volume("tests")
    if test_files > source_files or test_lines > source_lines:
        raise SystemExit("Test volume exceeds source volume")
    if args.base:
        output = subprocess.run(
            ["git", "diff", "--numstat", args.base, "--", "src/", "tests/"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        changes = {"src": 0, "tests": 0}
        for line in output.splitlines():
            added, removed, path = line.split("\t", 2)
            if added != "-":
                changes[path.split("/", 1)[0]] += int(added) + int(removed)
        if changes["tests"] > 2 * changes["src"]:
            raise SystemExit("Test diff exceeds twice the source diff")
    print(f"Test budget: {test_files}/{source_files} files, {test_lines}/{source_lines} lines")


if __name__ == "__main__":
    main()

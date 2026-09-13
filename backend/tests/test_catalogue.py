"""The test catalogue must describe the tests that exist (TR-213).

`docs/TEST_CASES.md` is the index a reviewer reads instead of the suite. An index that names a
test which was renamed, or omits one that carries an ID, is worse than no index: it reads as
verification and is not. This module parses the catalogue and resolves every row against the tree,
so that renaming a test without touching its row fails here rather than in somebody's reading of
the documents two days after release, which is how the four rows fixed on 2026-09-13 were found.

Nothing here calls a model or touches the network; it is markdown, `ast`, and file reads.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CATALOGUE = REPO / "docs" / "TEST_CASES.md"
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"

ROW = re.compile(r"^\|\s*(TC-[A-Z0-9]+-\d+)\s*\|")
"""A catalogue row: the first cell is the case ID."""

BACKTICKED = re.compile(r"`([^`]+)`")
"""Inline code spans; a row's location column names its tests in them."""

HEADING_PATH = re.compile(r"`((?:backend/)?tests/[\w/]+\.py|(?:frontend/)?(?:src|e2e)/[\w/.]+)`")
"""A path named in a section heading, which the rows beneath it abbreviate to ``::name``."""

DOCSTRING_ID = re.compile(r"^(TC-[A-Z0-9]+-\d+)")
"""The ID a backend test claims on the first line of its docstring (CLAUDE.md §4.7)."""

NO_LOCATION = {"—", "-", ""}
"""Location cells for rows that name no test: the manual checklist."""


def _cells(line: str) -> list[str]:
    """Split a markdown table row into its cells.

    Args:
        line: One line of the catalogue.

    Returns:
        The cells, without the leading and trailing empties.
    """
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _rows() -> list[tuple[int, str, str, str]]:
    """Read every catalogue row with the file its location column abbreviates.

    Returns:
        One ``(line number, case id, location cell, status cell)`` per row, with ``::name``
        locations already resolved against the nearest preceding explicit path.
    """
    found: list[tuple[int, str, str, str]] = []
    context: str | None = None
    for number, line in enumerate(CATALOGUE.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("#"):
            heading = HEADING_PATH.search(line)
            if heading:
                context = heading.group(1)
            continue
        if not ROW.match(line):
            continue
        cells = _cells(line)
        # A row is `| id | requirement | then | location | status |`, but earlier cells may
        # contain a pipe inside prose, so the location is counted from the right.
        if len(cells) < 3:
            continue
        case_id, location, status = cells[0], cells[-2], cells[-1]
        for reference in BACKTICKED.findall(location):
            if "::" in reference and not reference.startswith("::"):
                context = reference.split("::", 1)[0]
        found.append((number, case_id, location, status))
        if context is not None:
            found[-1] = (number, case_id, location.replace("`::", f"`{context}::"), status)
    return found


def _backend_tests() -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Collect backend test functions and the IDs their docstrings claim.

    Returns:
        A mapping of repository-relative path to the test names it defines, and a mapping of
        case ID to the ``path::name`` of every test claiming it.
    """
    defined: dict[str, set[str]] = {}
    claimed: dict[str, list[str]] = {}
    for path in sorted((BACKEND / "tests").rglob("test_*.py")):
        relative = str(path.relative_to(BACKEND))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test"):
                continue
            names.add(node.name)
            match = DOCSTRING_ID.match((ast.get_docstring(node) or "").strip())
            if match:
                claimed.setdefault(match.group(1), []).append(f"{relative}::{node.name}")
        defined[relative] = names
    return defined, claimed


def _frontend_sources() -> dict[str, str]:
    """Read every frontend test file.

    Returns:
        A mapping of repository-relative path to the file's text, which test titles are
        searched in directly: a title is a string, not a symbol.
    """
    sources: dict[str, str] = {}
    roots = (
        (FRONTEND / "src", ("*.test.ts", "*.test.tsx")),
        (FRONTEND / "e2e", ("*.spec.ts",)),
    )
    for root, patterns in roots:
        for pattern in patterns:
            for path in sorted(root.rglob(pattern)):
                sources[str(path.relative_to(FRONTEND))] = path.read_text(encoding="utf-8")
    return sources


CATALOGUE_ROWS = _rows()
BACKEND_DEFINED, BACKEND_CLAIMED = _backend_tests()
FRONTEND_SOURCES = _frontend_sources()


def test_the_catalogue_is_not_empty_and_parses() -> None:
    """TC-BE-353: a parser that silently matches nothing would pass every check below."""
    assert len(CATALOGUE_ROWS) > 400
    assert len(BACKEND_DEFINED) >= 20
    assert sum(len(names) for names in BACKEND_DEFINED.values()) > 300
    assert len(FRONTEND_SOURCES) >= 18


def test_every_row_names_a_test_that_exists() -> None:
    """TC-BE-354: TR-213 -- a row naming a renamed test is a claim of coverage that is not there."""
    unresolved: list[str] = []
    for number, case_id, location, _status in CATALOGUE_ROWS:
        if case_id.startswith("TC-MAN") or location in NO_LOCATION:
            continue
        for reference in BACKTICKED.findall(location):
            if "::" not in reference:
                continue
            path, _, name = reference.partition("::")
            path = path.removeprefix("backend/").removeprefix("frontend/")
            name = name.strip().strip('"')
            if path.endswith(".py"):
                names = BACKEND_DEFINED.get(path)
                if names is None:
                    unresolved.append(f"{CATALOGUE.name}:{number} {case_id}: no file {path}")
                elif name.startswith("test") and name not in names:
                    unresolved.append(f"{CATALOGUE.name}:{number} {case_id}: {path} has no {name}")
            else:
                source = FRONTEND_SOURCES.get(path)
                if source is None:
                    unresolved.append(f"{CATALOGUE.name}:{number} {case_id}: no file {path}")
                elif name not in source:
                    unresolved.append(
                        f"{CATALOGUE.name}:{number} {case_id}: {path} has no {name!r}"
                    )
    assert not unresolved, "\n".join(unresolved)


def test_every_backend_test_id_has_a_row_and_no_id_is_claimed_twice() -> None:
    """TC-BE-355: TR-213 -- an ID in a docstring with no row is a test nobody can find."""
    catalogued = {case_id for _, case_id, _, _ in CATALOGUE_ROWS}
    orphans = sorted(set(BACKEND_CLAIMED) - catalogued)
    assert not orphans, f"backend tests claim IDs with no catalogue row: {orphans}"

    duplicates = {
        case_id: owners
        for case_id, owners in BACKEND_CLAIMED.items()
        if len({owner.split("::")[0] for owner in owners}) > 1
    }
    assert not duplicates, f"one ID claimed by tests in different files: {duplicates}"


@pytest.mark.parametrize("prefix", ["TC-BE", "TC-FE"])
def test_no_catalogue_id_is_used_by_two_rows(prefix: str) -> None:
    """TC-BE-356: TR-213 -- a reused ID makes two different behaviours look like one."""
    seen: dict[str, int] = {}
    repeated: list[str] = []
    for number, case_id, _, _ in CATALOGUE_ROWS:
        if not case_id.startswith(prefix):
            continue
        if case_id in seen:
            repeated.append(f"{case_id}: lines {seen[case_id]} and {number}")
        seen[case_id] = number
    assert not repeated, "\n".join(repeated)

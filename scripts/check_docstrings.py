#!/usr/bin/env python3
"""Fail if any module, class, or function lacks documentation.

Python (``.py``): every module, class, and function needs a docstring, including private (``_name``) and
nested functions, which ruff's pydocstyle rules skip. Test functions (``test_*`` in ``test_*.py``) are
exempt: their names describe the behavior.

TypeScript (``.ts``/``.tsx``): every named ``function`` declaration (top-level or nested) and every
``class`` needs a JSDoc block (``/** ... */``) directly above it. Test files (``*.test.ts``) are exempt.

Usage: ``python scripts/check_docstrings.py src tests web/src``
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_TS_DECL = re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?(function\s+\w+|class\s+\w+)")


def missing_docstrings(path: Path) -> list[str]:
    """Return ``file:line kind name`` entries for every undocumented Python definition in ``path``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    is_test_module = path.name.startswith("test_")
    out: list[str] = []
    if path.stat().st_size and ast.get_docstring(tree) is None:
        out.append(f"{path}:1 module")
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if is_test_module and node.name.startswith("test_"):
            continue
        if ast.get_docstring(node) is None:
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            out.append(f"{path}:{node.lineno} {kind} {node.name}")
    return out


def missing_jsdoc(path: Path) -> list[str]:
    """Return ``file:line declaration`` entries for TypeScript functions and classes without JSDoc."""
    out: list[str] = []
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        match = _TS_DECL.match(line)
        if match and not (i > 0 and lines[i - 1].strip().endswith("*/")):
            out.append(f"{path}:{i + 1} {match.group(4)}")
    return out


def main(argv: list[str]) -> int:
    """Check every source file under the given paths and report missing documentation."""
    problems: list[str] = []
    for root in argv or ["src"]:
        for file in sorted(Path(root).rglob("*")):
            if "node_modules" in file.parts or not file.is_file():
                continue
            if file.suffix == ".py":
                problems += missing_docstrings(file)
            elif file.suffix in (".ts", ".tsx") and not file.name.endswith((".test.ts", ".d.ts")):
                problems += missing_jsdoc(file)
    for p in problems:
        print(p)
    print(f"{len(problems)} definition(s) without documentation", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

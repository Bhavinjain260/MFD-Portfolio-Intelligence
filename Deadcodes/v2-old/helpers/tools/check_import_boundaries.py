#!/usr/bin/env python3
"""Fail if core/ingest/jobs import streamlit or ui."""
from __future__ import annotations

import ast
import pathlib
import sys

VIOLATIONS = []
LAYERS = {
    "core": {"forbidden": {"streamlit", "ui", "ingest", "jobs"}},
    "ingest": {"forbidden": {"streamlit", "ui"}},
    "jobs": {"forbidden": {"streamlit", "ui"}},
}

root = pathlib.Path(__file__).resolve().parent.parent

for layer, rules in LAYERS.items():
    layer_path = root / layer
    if not layer_path.exists():
        continue
    for path in layer_path.rglob("*.py"):
        if path.name.startswith("_") and path.name != "__init__.py":
            pass
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            VIOLATIONS.append(f"{path}: syntax error {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    top = n.name.split(".")[0]
                    if top in rules["forbidden"]:
                        VIOLATIONS.append(f"{path}: imports {n.name}")
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if top in rules["forbidden"]:
                    VIOLATIONS.append(f"{path}: from {node.module}")

if VIOLATIONS:
    print("\n".join(VIOLATIONS))
    sys.exit(1)
print("OK — import boundaries clean")

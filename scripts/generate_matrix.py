#!/usr/bin/env python3
"""Read images/*.yml and emit a JSON matrix for GitHub Actions strategy.matrix.include."""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def main() -> None:
    matrix = []
    for f in sorted(Path("images").glob("*.yml")):
        data = yaml.safe_load(f.read_text())
        matrix.append(data)
    print(json.dumps(matrix))


if __name__ == "__main__":
    main()

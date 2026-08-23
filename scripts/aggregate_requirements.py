#!/usr/bin/env python3
"""Aggregate all requirements.txt files from src/ and ocb/ into requirements/aggregated.txt.

Strategy:
  - Exact pins (==x.y.z) are widened to minimum-version pins (>=x.y.z).
  - Upper bounds (<x.y.z, <=x.y.z) are dropped entirely.
    Individual module requirements.txt files use upper bounds to mean "tested up to here",
    not "will break above here". When aggregating dozens of repos spanning years, these
    upper bounds become mutually contradictory (e.g. cryptography<23 vs cryptography>=43).
  - When multiple files produce the same package with multiple >=x.y.z constraints,
    only the highest minimum is kept.
  - != exclusions and ==X.Y.* wildcards are preserved.
  - Packages that are stdlib in Python 3.7+ or require unavailable system libs are dropped.

Run from the repo root after the clone step:
    python scripts/aggregate_requirements.py
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

_PKG_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)"          # package name
    r"(?P<extras>\[[^\]]*\])?"              # optional [extras]
    r"(?P<spec>[^;#\s]*)?"                  # optional specifier (>=1.0,<2, ==1.0, …)
    r"(?P<marker>\s*;[^#]*)?"              # optional ; marker
    r"(?P<comment>\s*#.*)?$",              # optional inline comment
    re.ASCII,
)

# Packages that are stdlib in Python 3.7+ or have no valid PyPI release.
_SKIP_PACKAGES: frozenset[str] = frozenset({
    "dataclasses",   # stdlib since 3.7; PyPI backport only 0.1–0.6 (Python 3.6)
    "mysqlclient",  # no valid PyPI releases; mysqlclient is the correct package name
})


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _loosen(spec: str) -> str:
    """Normalize a specifier string for aggregation.

    - ==X.Y.Z  → >=X.Y.Z  (exact pin → lower bound)
    - ==X.Y.*  stays ==X.Y.* (wildcard, valid PEP 440)
    - <X, <=X  → dropped   (upper bounds conflict across repos; see module docstring)
    - ~=X.Y.Z  → >=X.Y.Z  (compatible release → lower bound)
    - >=X, !=X stay unchanged
    """
    parts = [s.strip() for s in spec.split(",") if s.strip()]
    result_parts: list[str] = []
    for part in parts:
        # Drop upper bounds
        if re.match(r"^<=?[\d]", part):
            continue
        # Convert ~=X.Y.Z to >=X.Y.Z
        part = re.sub(r"^~=(\d)", r">=\1", part)
        # Convert ==X.Y.Z to >=X.Y.Z (but not ==X.Y.* wildcards)
        if not part.endswith(".*"):
            part = re.sub(r"(?<![!~<>])={2}(\d)", r">=\1", part)
        result_parts.append(part)
    return ",".join(result_parts)


def _max_ge_version(specs: list[str]) -> tuple[int, ...]:
    """Return the numeric tuple of the highest >= version found in specs."""
    best: tuple[int, ...] = (0,)
    for s in specs:
        m = re.search(r">=(\d[\d.]*)", s)
        if m:
            v = tuple(int(x) for x in m.group(1).split(".") if x.isdigit())
            if v > best:
                best = v
    return best


def _parse_req_file(
    path: Path, groups: dict, skip: frozenset, *, verbatim: bool = False
) -> None:
    """Parse a requirements-style file into `groups`.

    `verbatim=True` (used for override files) keeps each specifier exactly
    as written — no widening of exact pins, no dropping of upper bounds.
    Override files are a hand-authored final decision for that package
    (sometimes an explicit range like ">=38,<43" to keep a known-bad newer
    release out), not one more heterogeneous old requirements.txt to run
    through the "loosen and reconcile dozens of repos" heuristic that
    exists for everything else `_parse_req_file` sees.
    """
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Direct references — local paths (./x, /x, ../x), editable installs
        # (-e), includes (-r/-c), or VCS/URL installs (git+https://…) — are
        # not ordinary name+specifier requirements. _PKG_LINE_RE's [A-Za-z0-9_.-]+
        # name group happily (mis)matches a leading "." in "./addons/..." as a
        # bogus package name, corrupting the aggregated file with garbage like
        # "-/addons/iot_box_image/...whl" that pip then rejects outright.
        if line.startswith(("-e", "-r", "-c", "./", "../", "/")) or "://" in line:
            continue
        m = _PKG_LINE_RE.match(line)
        if not m:
            continue
        name = _normalize(m.group("name"))
        if name in skip:
            continue
        extras = (m.group("extras") or "").strip()
        raw_spec = (m.group("spec") or "").strip()
        spec = raw_spec if verbatim else _loosen(raw_spec)
        marker = (m.group("marker") or "").strip()
        groups[(name, extras, marker)].add(spec)


def main() -> None:
    import os

    odoo_version = os.environ.get("ODOO_VERSION", "")

    # key: (normalized_name, extras_str, marker_str) → set of loosened specifier strings
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    sources: list[Path] = []

    for req in sorted(Path(".").rglob("requirements.txt")):
        parts = req.parts
        if any(
            p in {".git", "node_modules", ".venv", "scripts", "requirements"}
            for p in parts
        ):
            continue
        sources.append(req)
        _parse_req_file(req, groups, _SKIP_PACKAGES)

    # Per-version overrides: requirements/overrides-{odoo_version}.txt
    # Each line here REPLACES the aggregated constraints for that package —
    # for the *package*, not for the exact (extras, marker) combination the
    # aggregator happened to group it under. Two repos can declare the same
    # package under different environment markers (e.g. one plain, one
    # "; python_version >= ...") and land in different `groups` keys; if the
    # override only replaced the one matching key, the other marker variant
    # would survive untouched and reintroduce the exact conflict the override
    # was written to resolve (seen with idna: an override pinning it to 3.4
    # left a marker-qualified "idna>=3.6" from another repo standing right
    # next to it in aggregated.txt, still an unconditional requirement in this
    # environment). Delete every existing group for an overridden package
    # name — any extras/marker variant — before inserting the override's own.
    if odoo_version:
        override_path = Path("requirements") / f"overrides-{odoo_version}.txt"
        if override_path.exists():
            overrides: dict[tuple[str, str, str], set[str]] = defaultdict(set)
            _parse_req_file(override_path, overrides, frozenset(), verbatim=True)
            override_names = {name for (name, _extras, _marker) in overrides}
            for key in [k for k in groups if k[0] in override_names]:
                del groups[key]
            for key, specs in overrides.items():
                groups[key] = specs
            print(f"Applied overrides from {override_path}")

    if not groups:
        print("No requirements found — is src/ or ocb/ populated?", file=sys.stderr)
        sys.exit(1)

    out_lines: list[str] = []
    for (name, extras, marker), specs in sorted(groups.items()):
        # After _loosen, specs contain only >=X, !=X, ==X.*, or compounds thereof
        # — except override-sourced specs (verbatim=True), which can be anything
        # an override author wrote (e.g. an exact ==X or a >=X,<Y range) and pass
        # through untouched. Either way, separate pure >=X.Y.Z from everything else.
        ge_only = [s for s in specs if re.fullmatch(r">=[\d.]+", s)]
        other = [s for s in specs if s not in ge_only]

        chosen_specs: list[str] = list(other)
        if ge_only:
            # Keep only the highest >=X.Y.Z minimum; lower minimums are redundant.
            best = max(ge_only, key=lambda s: _max_ge_version([s]))
            chosen_specs.append(best)

        for spec in sorted(chosen_specs) or [""]:
            line = f"{name}{extras}{spec}"
            if marker:
                line += f" {marker}"
            out_lines.append(line)

    Path("requirements").mkdir(exist_ok=True)
    out = Path("requirements/aggregated.txt")
    out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"Aggregated {len(out_lines)} requirements from {len(sources)} files → {out}")


if __name__ == "__main__":
    main()

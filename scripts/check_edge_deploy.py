"""Diff a deployment by hash, not by eyeballing directories.

Write a manifest of every file the edge package needs, then verify a copied tree
(or a Pi) against it:

    python scripts/check_edge_deploy.py --manifest edge_manifest.sha256 \
        --roots simulation scripts                # write it (first run)
    python scripts/check_edge_deploy.py --manifest edge_manifest.sha256 \
        --roots simulation scripts                # verify it (subsequent runs)
    python scripts/check_edge_deploy.py --manifest edge_manifest.sha256 --update

The manifest format is the ``sha256sum -c`` one, so the same file verifies with
``sha256sum -c edge_manifest.sha256`` on the Pi.

Stdlib only; safe to launch from any working directory.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Build artefacts and caches that must never enter the manifest.
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(roots: list[str], suffixes: tuple[str, ...]) -> dict[str, str]:
    """Map POSIX-style relative path -> sha256 for every candidate file."""
    files: dict[str, str] = {}
    for root in roots:
        base = (ROOT / root).resolve()
        if not base.exists():
            print(f"! root {root!r} does not exist", file=sys.stderr)
            continue
        targets = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in targets:
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            if suffixes and path.suffix not in suffixes:
                continue
            files[path.relative_to(ROOT).as_posix()] = sha256_file(path)
    return files


def read_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        if not name:
            digest, _, name = line.partition(" *")
        entries[name.strip()] = digest.strip()
    return entries


def write_manifest(path: Path, files: dict[str, str]) -> None:
    body = "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    path.write_text(body, encoding="utf-8")
    print(f"wrote {len(files)} entries to {path}")


def main() -> int:
    p = argparse.ArgumentParser(description="hash a deployment and verify a copy against it")
    p.add_argument("--manifest", default=str(ROOT / "edge_manifest.sha256"))
    p.add_argument("--roots", nargs="+", default=["simulation", "scripts"])
    p.add_argument("--suffixes", nargs="*", default=[".py"], help="file types to include (default: .py)")
    p.add_argument("--update", action="store_true", help="rewrite the manifest instead of verifying")
    args = p.parse_args()

    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = ROOT / manifest
    suffixes = tuple(args.suffixes or ())

    current = collect(args.roots, suffixes)

    if args.update or not manifest.is_file():
        write_manifest(manifest, current)
        return 0

    expected = read_manifest(manifest)
    missing = sorted(set(expected) - set(current))
    extra = sorted(set(current) - set(expected))
    changed = sorted(name for name in set(expected) & set(current) if expected[name] != current[name])

    if not (missing or extra or changed):
        print(f"OK: {len(expected)} files match {manifest}")
        return 0

    for name in missing:
        print(f"MISSING  {name}")
    for name in changed:
        print(f"CHANGED  {name}")
    for name in extra:
        print(f"EXTRA    {name}")
    print(f"FAIL: {len(missing)} missing, {len(changed)} changed, {len(extra)} extra")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Schema migrations: bring the canon on disk up to the code's SCHEMA_VERSION.

A breaking schema change (new required field, renamed/removed field, changed meaning)
bumps schemas.version.SCHEMA_VERSION and appends a Migration here, in the same PR. Each
migration is a pure frontmatter transform; running `python -m tools.migrate` applies
every pending one to every canon file, rewrites the files canonically, and records the
new version in .schema-version. The validator refuses to run when the recorded version
and SCHEMA_VERSION disagree, so a breaking change can never be committed half-applied.

Rules for the MIGRATIONS list:
  * Append only. Never edit or reorder a migration that has shipped — its transform is
    part of the historical record other checkouts replay.
  * A migration transforms (frontmatter dict, type_name) -> new frontmatter dict. It must
    be idempotent-safe over its own version step and touch only frontmatter; prose bodies
    are passed through untouched.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import frontmatter

from schemas.version import SCHEMA_VERSION

from .common import CANON_DIR, SCHEMA_VERSION_FILE
from .fmt import canonical_text


@dataclass(frozen=True)
class Migration:
    to_version: int                                   # version this migration produces
    description: str
    transform: Callable[[dict, str], dict]            # (meta, type_name) -> new meta


# Ordered, append-only. Empty at fork time: this repo's schema starts at v1
# (schemas/version.py), and the ancestor spine's migrations describe a schema this
# canon never had, so they were not carried over. The first breaking change here ships
# the first Migration.
MIGRATIONS: list[Migration] = []


def read_canon_version(path: Path = SCHEMA_VERSION_FILE) -> int:
    """The schema version the canon on disk is recorded at (0 if unrecorded)."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_canon_version(version: int, path: Path = SCHEMA_VERSION_FILE) -> None:
    path.write_text(f"{version}\n")


def pending_migrations(current: int, target: int = SCHEMA_VERSION,
                       migrations: list[Migration] | None = None) -> list[Migration]:
    migrations = MIGRATIONS if migrations is None else migrations
    return sorted((m for m in migrations if current < m.to_version <= target),
                  key=lambda m: m.to_version)


def migrate(canon_dir: Path = CANON_DIR, *, target: int = SCHEMA_VERSION,
            migrations: list[Migration] | None = None,
            version_file: Path = SCHEMA_VERSION_FILE) -> int:
    """Apply pending migrations to every canon file, then record `target`."""
    current = read_canon_version(version_file)
    if current == target:
        print(f"canon already at schema v{target}; nothing to do.")
        return 0
    if current > target:
        print(f"canon records schema v{current} but code expects v{target}; "
              f"update the code — do not downgrade canon.")
        return 1

    pend = pending_migrations(current, target, migrations)
    changed = 0
    for path in sorted(canon_dir.rglob("*.md")):
        post = frontmatter.load(path)
        meta = dict(post.metadata)
        if not meta.get("id") or not meta.get("type"):
            continue  # not a well-formed entity; leave for the validator
        for m in pend:
            meta = m.transform(meta, meta.get("type"))
        path.write_text(canonical_text(meta.get("type"), meta, post.content))
        changed += 1

    write_canon_version(target, version_file)
    for m in pend:
        print(f"  applied v{m.to_version}: {m.description}")
    print(f"Migrated canon v{current} -> v{target} across {changed} file(s). "
          f"Run `make build` and commit.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply pending schema migrations to canon.")
    parser.add_argument("--check", action="store_true",
                        help="report whether migrations are pending, without writing")
    args = parser.parse_args(argv)

    current = read_canon_version()
    if args.check:
        pend = pending_migrations(current)
        if current == SCHEMA_VERSION and not pend:
            print(f"OK — canon at schema v{current}.")
            return 0
        print(f"canon at v{current}, code expects v{SCHEMA_VERSION}: "
              f"{len(pend)} migration(s) pending. Run `make migrate`.")
        return 1
    return migrate()


if __name__ == "__main__":
    sys.exit(main())

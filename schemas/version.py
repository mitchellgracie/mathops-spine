"""The canon schema version — the single integer the migration policy turns on.

The first time a required field is added to an entity type, every existing file breaks.
The policy that keeps the schema evolving instead of ossifying out of fear:

  * ADDITIVE changes (a new field with a default) do NOT bump this. Old canon still
    validates, so no migration is needed — prefer this shape whenever possible.
  * A BREAKING change (a new required field, a renamed or removed field, a changed
    meaning) bumps SCHEMA_VERSION and ships a migration in tools/migrate, in the SAME PR,
    that rewrites canon to match.

The validator refuses to run when the canon on disk records a different version than the
code expects (see tools.migrate.read_canon_version and validate.check_schema_version), so
a breaking change can never land half-applied.

This fork restarts at 1: the ancestor spine's v1->v2 history describes a schema this
repo never had, so its migrations were not carried over (tools/migrate.py's MIGRATIONS
list starts empty here).
"""
from __future__ import annotations

SCHEMA_VERSION = 1

"""Load the canon from disk into typed model instances.

Returns a Canon object holding:
  * entities: id -> model instance
  * bodies:   id -> prose body (the markdown under the frontmatter)
  * errors:   list of (path, message) for files that failed to load/validate

Loading does NOT raise on a bad file; it collects the error so validate.py can
report all problems at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
from pydantic import ValidationError

from schemas import Entity
from schemas.registry import model_for_prefix, model_for_type

from .common import CANON_DIR


@dataclass
class Canon:
    entities: dict[str, Entity] = field(default_factory=dict)
    bodies: dict[str, str] = field(default_factory=dict)
    paths: dict[str, Path] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def ids(self) -> set[str]:
        return set(self.entities)


def _id_prefix(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def load_canon(canon_dir: Path = CANON_DIR) -> Canon:
    canon = Canon()
    for path in sorted(canon_dir.rglob("*.md")):
        rel = str(path.relative_to(canon_dir.parent))
        try:
            post = frontmatter.load(path)
        except Exception as exc:  # malformed YAML, etc.
            canon.errors.append((rel, f"could not parse frontmatter: {exc}"))
            continue

        meta = dict(post.metadata)
        type_name = meta.get("type")
        entity_id = meta.get("id")

        if not entity_id:
            canon.errors.append((rel, "missing required field: id"))
            continue
        if not type_name:
            canon.errors.append((rel, "missing required field: type"))
            continue

        model = model_for_type(type_name)
        if model is None:
            canon.errors.append((rel, f"unknown entity type: {type_name!r}"))
            continue

        # id prefix must match the declared type (typed pointers)
        prefix = _id_prefix(entity_id)
        expected = model.id_prefix
        if prefix != expected:
            canon.errors.append(
                (rel, f"id prefix {prefix!r} does not match type {type_name!r} "
                      f"(expected prefix {expected!r}, e.g. {expected}.<slug>)")
            )
            continue

        try:
            entity = model(**meta)
        except ValidationError as exc:
            canon.errors.append((rel, f"schema validation failed:\n{_fmt(exc)}"))
            continue

        if entity_id in canon.entities:
            prior = canon.paths[entity_id]
            canon.errors.append((rel, f"duplicate id {entity_id!r} (already defined in {prior})"))
            continue

        canon.entities[entity_id] = entity
        canon.bodies[entity_id] = post.content
        canon.paths[entity_id] = path
    return canon


def _fmt(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"    - {loc}: {err['msg']}")
    return "\n".join(lines)

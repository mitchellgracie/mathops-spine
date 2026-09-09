"""Single place that knows every entity type. Add new types here."""
from __future__ import annotations

from .base import Entity
from .entities import (
    Computation,
    Definition,
    MathObject,
    Proof,
    Source,
    Statement,
    Technique,
)

ENTITY_TYPES: list[type[Entity]] = [
    Definition,
    Statement,
    Proof,
    MathObject,
    Source,
    Computation,
    Technique,
]

BY_TYPE_NAME: dict[str, type[Entity]] = {c.type_name: c for c in ENTITY_TYPES}
BY_PREFIX: dict[str, type[Entity]] = {c.id_prefix: c for c in ENTITY_TYPES}


def model_for_type(type_name: str) -> type[Entity] | None:
    return BY_TYPE_NAME.get(type_name)


def model_for_prefix(prefix: str) -> type[Entity] | None:
    return BY_PREFIX.get(prefix)

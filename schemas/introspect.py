"""Walk a model's type hints and report every reference field and its target type.

Robust to Optional[...], list[...], and Annotated[...] nesting. We read the raw hints
with include_extras=True so we don't depend on where Pydantic stashes metadata.
"""
from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Annotated, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from .base import Entity, Ref


@dataclass(frozen=True)
class RefField:
    field: str          # frontmatter key
    target_type: str    # entity type it must resolve to ("*" == any)
    is_list: bool       # True if the field is a list of refs
    # Set when the ref is buried inside a nested model field (e.g. IntentionalConflict.target
    # inside `intentional_conflicts: list[IntentionalConflict]`) rather than directly on the
    # entity: names the attribute on each list item that holds the ref. None for a plain
    # scalar/list-of-str ref field like `culture` or `affiliations`.
    subfield: str | None = None


def _find_ref(annotation) -> Ref | None:
    """Return the Ref marker buried anywhere in an annotation, else None."""
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, Ref):
                return meta
        return _find_ref(args[0])
    if origin in (typing.Union,):  # Optional[X] == Union[X, None]
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _find_ref(arg)
            if found:
                return found
    return None


def _is_listish(annotation) -> bool:
    origin = get_origin(annotation)
    if origin in (list, set, tuple, frozenset):
        return True
    if origin is Annotated:
        return _is_listish(get_args(annotation)[0])
    if origin is typing.Union:
        return any(_is_listish(a) for a in get_args(annotation) if a is not type(None))
    return False


def _inner_of_list(annotation):
    """Return the element annotation if this is a (possibly wrapped) list type."""
    origin = get_origin(annotation)
    if origin in (list, set, tuple, frozenset):
        return get_args(annotation)[0]
    if origin is Annotated:
        return _inner_of_list(get_args(annotation)[0])
    if origin is typing.Union:
        for a in get_args(annotation):
            if a is type(None):
                continue
            inner = _inner_of_list(a)
            if inner is not None:
                return inner
    return None


def _nested_model(annotation) -> type[BaseModel] | None:
    """Return the BaseModel class named by an annotation, unwrapping Optional/Annotated.

    A Ref buried on a *field of a nested model* (IntentionalConflict.target, reached via
    `intentional_conflicts: list[IntentionalConflict]`) is invisible to `_find_ref`, which
    only looks for a Ref marker directly on the annotation it is given. This is the other
    half of the walk: recognize "this field's value is itself a model" so `ref_fields` can
    recurse one level into it. Plain scalar/date models (e.g. WorldDate) simply have no Ref
    fields inside, so recursing into them is harmless — it just finds nothing.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _nested_model(get_args(annotation)[0])
    if origin is typing.Union:
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _nested_model(arg)
            if found is not None:
                return found
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def ref_fields(model_cls: type[Entity]) -> list[RefField]:
    out: list[RefField] = []
    hints = get_type_hints(model_cls, include_extras=True)
    for name, hint in hints.items():
        if name in ("relations",):  # relations handled separately (Relation.target)
            continue
        is_list = _is_listish(hint)
        search = _inner_of_list(hint) if is_list else hint
        if search is None:
            continue
        marker = _find_ref(search)
        if marker is not None:
            out.append(RefField(field=name, target_type=marker.target_type, is_list=is_list))
            continue
        # Not a direct ref -- check whether this field's value is itself a model (e.g.
        # IntentionalConflict) with its own Ref-typed fields, one level down.
        nested = _nested_model(search)
        if nested is None:
            continue
        for sub_name, sub_hint in get_type_hints(nested, include_extras=True).items():
            sub_marker = _find_ref(sub_hint)
            if sub_marker is not None:
                out.append(RefField(field=name, target_type=sub_marker.target_type,
                                    is_list=is_list, subfield=sub_name))
    return out

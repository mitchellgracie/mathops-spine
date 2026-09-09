"""Canonical formatter for canon files. Run as: python -m tools.fmt  [--check]

Git is this repo's transaction log and multiple authors — soon including agents — commit
concurrently. Unnormalized frontmatter (key order, quoting, list layout, line wrapping)
makes diffs noisy and YAML merges conflict-prone. This is the canon-side twin of the
deterministic derived serialization in tools.build: it rewrites every canon file to one
byte-stable form so diffs stay semantic and merges stay mostly automatic.

Design choices, and why:
  * Canonical field order IS the schema's declaration order (`model.model_fields`). The
    formatter has no ordering policy of its own — to change how a canon file is laid out,
    reorder the fields in schemas/entities.py. Keys absent from the schema (there should
    be none in valid canon, since Entity forbids extras) are kept, after the known keys,
    in their existing order, so formatting never drops data even on an invalid file.
  * Values are re-emitted exactly as loaded (raw YAML primitives), NOT round-tripped
    through the Pydantic model. That keeps formatting a pure restyle: it must never
    inject defaults, coerce a date string into an object, or otherwise change meaning —
    so the derived artifacts are provably unaffected by a format pass.
  * A file that fails to parse is reported and left untouched, never mangled — the real
    problem is the validator's to report, not something to paper over.

`--check` writes nothing and exits non-zero if any file is not already canonical; that is
the form CI runs (the canon twin of the derived-drift byte-diff gate).
"""
from __future__ import annotations

import sys

import frontmatter
import yaml

from schemas.registry import model_for_type

from .common import CANON_DIR


class _CanonDumper(yaml.SafeDumper):
    """Indent block sequences under their parent key (the existing canon style).

    PyYAML by default renders a block sequence flush with its mapping key; overriding
    increase_indent to never go indentless restores the conventional two-space indent,
    so `relations:` items sit under the key the way they are authored today.
    """

    def increase_indent(self, flow=False, indentless=False):  # noqa: D401
        return super().increase_indent(flow, False)


def _dict_representer(dumper: _CanonDumper, data: dict):
    # Force the top-level frontmatter mapping to BLOCK style, always. With plain
    # default_flow_style=None a scalar-only entity (a tombstone, a minimal new file)
    # collapses to a single `{a: 1, b: 2}` line — valid YAML, but inconsistent and
    # diff-hostile. Nested maps (relation dicts) keep flow_style=None so they stay
    # inline, matching the hand-authored `- {type: sibling, target: ...}` style. The
    # depth counter distinguishes the root map (block) from nested ones (auto/inline).
    depth = getattr(dumper, "_map_depth", 0)
    dumper._map_depth = depth + 1
    try:
        flow = False if depth == 0 else None
        return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=flow)
    finally:
        dumper._map_depth = depth


def _str_representer(dumper: _CanonDumper, data: str):
    # Multi-line strings emit as literal block scalars (|). The `statement` field is
    # the reason: LaTeX is authored and read by humans, and a folded/quoted scalar full
    # of \n escapes would make every theorem unreadable in its own file. PyYAML's
    # emitter falls back to a quoted style on content a literal block can't represent
    # (e.g. trailing spaces on a line), so this is a style *preference*, never a
    # correctness risk — and it round-trips: load -> dump -> load is identity either
    # way, which is all the idempotency check below requires.
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_CanonDumper.add_representer(dict, _dict_representer)
_CanonDumper.add_representer(str, _str_representer)


def _dump_yaml(data: dict) -> str:
    # default_flow_style=None: scalar-only sequences stay compact ([the Warden]),
    # sequences holding maps (relations) become block lists of inline maps. The custom
    # dict representer above keeps the top-level mapping block regardless. width is set
    # very high so long summaries never soft-wrap into multi-line folded scalars
    # (wrapping is a top source of noisy diffs).
    return yaml.dump(
        data,
        Dumper=_CanonDumper,
        sort_keys=False,
        default_flow_style=None,
        allow_unicode=True,
        width=4096,
    )


def _ordered(type_name, meta: dict) -> dict:
    """Return meta re-keyed into canonical (schema-declared) order."""
    model = model_for_type(type_name) if type_name else None
    order = list(model.model_fields) if model is not None else []
    known = [k for k in order if k in meta]
    extra = [k for k in meta if k not in order]  # preserve original order; ideally empty
    return {k: meta[k] for k in known + extra}


def canonical_text(type_name, meta: dict, body: str) -> str:
    """Pure projection: (type, raw frontmatter, prose body) -> canonical file text.

    Kept separate from the file I/O so it is directly testable for idempotency.
    """
    fm = _dump_yaml(_ordered(type_name, meta))
    body = body.strip("\n")
    text = f"---\n{fm}---\n"
    if body:
        text += body + "\n"
    return text


def _canonical_for(path) -> tuple[str, str] | None:
    """(current text, canonical text) for one file, or None if it isn't a canon entity.

    Raises if the frontmatter is malformed. Returns None when the file parses but has no
    id/type — a file with no closing `---`, say, parses as all-body with empty metadata,
    and reformatting that would wrap the whole thing in `{}`. Anything that isn't clearly
    a well-formed entity is left untouched for the validator to report.
    """
    post = frontmatter.load(path)
    meta = dict(post.metadata)
    if not meta.get("id") or not meta.get("type"):
        return None
    return path.read_text(), canonical_text(meta.get("type"), meta, post.content)


def run(check: bool, canon_dir=CANON_DIR) -> int:
    changed: list = []
    errored: list[tuple] = []
    for path in sorted(canon_dir.rglob("*.md")):
        try:
            result = _canonical_for(path)
        except Exception as exc:  # malformed frontmatter — leave it for the validator
            errored.append((path, exc))
            continue
        if result is None:       # parsed, but not a well-formed entity — leave alone
            continue
        old, new = result
        if old == new:
            continue
        changed.append(path)
        if not check:
            path.write_text(new)

    for path, exc in errored:
        print(f"SKIP  {path.relative_to(canon_dir.parent)}: could not parse ({exc})")

    rel = lambda p: p.relative_to(canon_dir.parent)  # noqa: E731
    if check:
        for path in changed:
            print(f"WOULD REFORMAT  {rel(path)}")
        if changed:
            print(f"\n{len(changed)} file(s) are not canonically formatted. Run 'make fmt'.")
            return 1
        print(f"OK — all canon files are canonically formatted "
              f"({len(errored)} unparseable, skipped).")
        return 1 if errored else 0

    for path in changed:
        print(f"reformatted  {rel(path)}")
    print(f"\nFormatted {len(changed)} file(s); {len(errored)} unparseable, skipped.")
    return 0


def main() -> int:
    check = "--check" in sys.argv[1:]
    return run(check=check)


if __name__ == "__main__":
    sys.exit(main())

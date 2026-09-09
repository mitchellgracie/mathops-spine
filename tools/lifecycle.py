"""Entity lifecycle: create, merge, and delete canon entities safely.

IDs are immutable, but entities are not eternal — duplicates get merged and drafts get
cut. Done by hand each of those is a multi-file edit the validator punishes afterward
(dangling references, a half-renamed link, a stale capsule). These three subcommands
make the mutation atomic and integrity-preserving, so referential-integrity errors go
back to being a rare signal instead of routine friction. This is also exactly the
mutation surface the Phase-3 agent layer needs.

  new     scaffold a schema-valid file, id-unique, born canonically formatted
  merge   fold a duplicate into a canonical entity: retarget every inbound reference,
          leave the duplicate as a tombstone (`merged_into`) so its id stays reserved
  delete  remove an entity, but only if nothing still points at it (or --force)

Merge and delete rewrite other files, so run `make build` afterward to refresh derived/,
and `make validate` to confirm the result. Merge is deliberately *mechanical*: it moves
references, it does not union prose or reconcile relation reciprocity — validate will
flag anything the author still needs to resolve by hand.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from schemas.introspect import ref_fields
from schemas.registry import model_for_type

from .common import CANON_DIR, CAPSULES_DIR
from .expositions import MENTION_RE, Exposition, load_expositions
from .fmt import canonical_text
from .loader import Canon, load_canon

# Where a new entity of each type is filed. The loader globs *.md regardless, so this is
# purely organizational — but keeping it explicit means `new` never guesses a plural.
DIR_FOR_TYPE = {
    "definition": "definitions",
    "statement": "statements",
    "proof": "proofs",
    "object": "objects",
    "source": "sources",
    "computation": "computations",
    "technique": "techniques",
}


@dataclass(frozen=True)
class Inbound:
    """One reference *into* a target entity: who holds it and where."""

    source: str   # entity id, or a scene path
    where: str    # field name / "relations[<type>]" / "body" / "scene"


def _mention_link_re(entity_id: str) -> re.Pattern:
    return re.compile(r"\[\[" + re.escape(entity_id) + r"\]\]")


def inbound_references(canon: Canon, target_id: str, *,
                       expositions: list[Exposition] | None = None) -> list[Inbound]:
    """Every reference pointing at `target_id`, across typed fields, relations, canon
    prose, and exposition files. The entity's own file is excluded. Shared by merge
    (what to retarget) and delete (what would dangle).
    """
    hits: list[Inbound] = []
    for eid, e in canon.entities.items():
        if eid == target_id:
            continue
        model = model_for_type(e.type)
        if model is not None:
            for rf in ref_fields(model):
                value = getattr(e, rf.field)
                targets = value if rf.is_list else ([value] if value is not None else [])
                if target_id in targets:
                    hits.append(Inbound(eid, rf.field))
        for rel in e.relations:
            if rel.target == target_id:
                hits.append(Inbound(eid, f"relations[{rel.type}]"))
        if target_id in MENTION_RE.findall(canon.bodies.get(eid, "")):
            hits.append(Inbound(eid, "body"))
    expositions = load_expositions() if expositions is None else expositions
    for x in expositions:
        if target_id in x.mentions:
            hits.append(Inbound(x.path, "exposition"))
    return hits


# --- new ---------------------------------------------------------------------

def _parse_sets(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got {pair!r}")
        key, val = pair.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def new_entity(type_name: str, slug: str, name: str, *, sets: dict | None = None,
               canon: Canon | None = None, canon_dir: Path = CANON_DIR) -> int:
    model = model_for_type(type_name)
    if model is None:
        print(f"Unknown entity type: {type_name!r} (see schemas/registry.py)")
        return 1
    entity_id = f"{model.id_prefix}.{slug}"

    if canon is None:
        canon = load_canon(canon_dir)
    if entity_id in canon.entities:
        print(f"id {entity_id!r} already exists ({canon.paths[entity_id]})")
        return 1

    meta = {"id": entity_id, "type": type_name, "name": name, **(sets or {})}
    try:                       # never write a file that would fail the schema
        model(**meta)
    except Exception as exc:
        print(f"Refusing to scaffold an invalid {type_name}:\n{exc}")
        return 1

    out_dir = canon_dir / DIR_FOR_TYPE.get(type_name, f"{type_name}s")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.md"
    if path.exists():
        print(f"file already exists: {path}")
        return 1
    path.write_text(canonical_text(type_name, meta, ""))
    print(f"created {path.relative_to(canon_dir.parent)}  (id {entity_id})")
    print("Next: fill in the body, then `make build && make bootstrap-capsules`.")
    return 0


# --- delete ------------------------------------------------------------------

def delete_entity(entity_id: str, *, force: bool = False, canon: Canon | None = None,
                  canon_dir: Path = CANON_DIR, capsules_dir: Path = CAPSULES_DIR) -> int:
    if canon is None:
        canon = load_canon(canon_dir)
    if entity_id not in canon.entities:
        print(f"no such entity: {entity_id}")
        return 1

    hits = inbound_references(canon, entity_id)
    if hits and not force:
        print(f"Refusing to delete {entity_id}: {len(hits)} inbound reference(s) remain.")
        for h in hits:
            print(f"  {h.source}  ({h.where})")
        print("Retarget or remove these first, or re-run with --force to leave them dangling.")
        return 1

    canon.paths[entity_id].unlink()
    cap = Path(capsules_dir) / f"{entity_id}.md"
    if cap.exists():
        cap.unlink()
    print(f"deleted {entity_id}" + (f" (forced; {len(hits)} reference(s) now dangling)" if hits else ""))
    return 0


# --- merge -------------------------------------------------------------------

def _dedup(seq: list, drop) -> list:
    seen, out = set(), []
    for x in seq:
        if x == drop or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _retarget_meta(meta: dict, old: str, new: str) -> bool:
    """Rewrite old->new in one file's frontmatter (typed refs + relations). Drops any
    reference that would now point at the file's own id (a self-reference). Returns
    whether anything changed."""
    self_id = meta.get("id")
    model = model_for_type(meta.get("type"))
    changed = False
    if model is not None:
        for rf in ref_fields(model):
            if rf.field not in meta:
                continue
            val = meta[rf.field]
            if rf.is_list and isinstance(val, list):
                nv = _dedup([new if x == old else x for x in val], drop=self_id)
                if nv != val:
                    meta[rf.field] = nv
                    changed = True
            elif val == old:
                if new == self_id:
                    del meta[rf.field]      # self-reference: drop it
                else:
                    meta[rf.field] = new
                changed = True
    rels = meta.get("relations")
    if isinstance(rels, list):
        new_rels = []
        for r in rels:
            if isinstance(r, dict) and r.get("target") == old:
                r = {**r, "target": new}
            if isinstance(r, dict) and r.get("target") == self_id:
                changed = True                # drop self-relation
                continue
            new_rels.append(r)
        if new_rels != rels:
            meta["relations"] = new_rels
            changed = True
    return changed


def _retarget_file(path: Path, old: str, new: str) -> bool:
    post = frontmatter.load(path)
    meta = dict(post.metadata)
    changed = _retarget_meta(meta, old, new)
    body = _mention_link_re(old).sub(f"[[{new}]]", post.content)
    if body != post.content:
        changed = True
    if changed:
        path.write_text(canonical_text(meta.get("type"), meta, body))
    return changed


def merge_entities(dup_id: str, canon_id: str, *, canon: Canon | None = None,
                   canon_dir: Path = CANON_DIR, capsules_dir: Path = CAPSULES_DIR,
                   expositions_dir=None) -> int:
    if canon is None:
        canon = load_canon(canon_dir)
    for eid in (dup_id, canon_id):
        if eid not in canon.entities:
            print(f"no such entity: {eid}")
            return 1
    if dup_id == canon_id:
        print("cannot merge an entity into itself")
        return 1
    if canon.entities[dup_id].merged_into is not None:
        print(f"{dup_id} is already a tombstone (merged into {canon.entities[dup_id].merged_into})")
        return 1
    if canon.entities[canon_id].merged_into is not None:
        print(f"{canon_id} is a tombstone; merge into its successor instead")
        return 1

    # 1. retarget every inbound reference dup -> canonical (canon files + prose)
    rewritten = 0
    for eid in canon.entities:
        if eid == dup_id:
            continue
        if _retarget_file(canon.paths[eid], dup_id, canon_id):
            rewritten += 1
    # 2. retarget exposition wiki-links dup -> canonical
    for exp in (load_expositions() if expositions_dir is None
                else load_expositions(expositions_dir)):
        xpath = (canon_dir.parent / exp.path)
        new_body = _mention_link_re(dup_id).sub(f"[[{canon_id}]]", exp.body)
        if new_body != exp.body:
            xpath.write_text(new_body)
            rewritten += 1
    # 3. turn the duplicate into a tombstone; drop its capsule (tombstones have none).
    #    Required (no-default) fields are carried over from the original so the tombstone
    #    still satisfies its own schema — e.g. a Proof tombstone must keep its `proves`.
    dup = canon.entities[dup_id]
    model = model_for_type(dup.type)
    dup_meta = dict(frontmatter.load(canon.paths[dup_id]).metadata)
    tomb = {"id": dup_id, "type": dup.type, "name": dup.name, "merged_into": canon_id}
    for fname, finfo in model.model_fields.items():
        if finfo.is_required() and fname not in tomb and fname in dup_meta:
            tomb[fname] = dup_meta[fname]
    canon.paths[dup_id].write_text(
        canonical_text(dup.type, tomb, f"Merged into [[{canon_id}]].")
    )
    cap = Path(capsules_dir) / f"{dup_id}.md"
    if cap.exists():
        cap.unlink()

    print(f"merged {dup_id} -> {canon_id}: retargeted {rewritten} file(s); "
          f"{dup_id} is now a tombstone.")
    print("Next: `make build`, then `make validate` to reconcile relations/prose by hand.")
    return 0


# --- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, merge, and delete canon entities.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="scaffold a schema-valid, id-unique entity file")
    p_new.add_argument("type", help="entity type (definition, statement, proof, ...); "
                                    "a proof also needs --set proves=thm.<slug>")
    p_new.add_argument("slug", help="id slug (the part after the prefix)")
    p_new.add_argument("--name", required=True, help="display name")
    p_new.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                       help="set an additional frontmatter field (repeatable)")

    p_merge = sub.add_parser("merge", help="fold DUPLICATE into CANONICAL (tombstone the former)")
    p_merge.add_argument("duplicate", help="id to retire")
    p_merge.add_argument("canonical", help="id to keep")

    p_del = sub.add_parser("delete", help="remove an entity if nothing references it")
    p_del.add_argument("id", help="entity id to delete")
    p_del.add_argument("--force", action="store_true",
                       help="delete even if inbound references remain (leaves them dangling)")

    args = parser.parse_args(argv)
    if args.cmd == "new":
        try:
            sets = _parse_sets(args.set)
        except ValueError as exc:
            print(exc)
            return 1
        return new_entity(args.type, args.slug, args.name, sets=sets)
    if args.cmd == "merge":
        return merge_entities(args.duplicate, args.canonical)
    if args.cmd == "delete":
        return delete_entity(args.id, force=args.force)
    return 1


if __name__ == "__main__":
    sys.exit(main())

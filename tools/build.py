"""Compile canon into the DETERMINISTIC derived/ artifacts. Run as: python -m tools.build

Outputs (all a pure function of canon + expositions; never hand-edit):
  derived/graph.json            entity nodes + labelled edges
  derived/indices/relations.json    adjacency by entity
  derived/indices/mentions.json     reverse index of [[id]] links in canon prose bodies
  derived/indices/appearances.json  reverse index of [[id]] links in exposition files
                                    (entity -> exposition paths)
  derived/indices/tags.json         reverse index of exposition material tags (tag ->
                                    exposition paths); the vocabulary is tools.taxonomy
  derived/indices/canonicity.json   id -> {state, weight}, the canonicity tier every
                                    entity carries (schemas.base.canon_weight); powers
                                    tools.assemble_context's --max-canon-weight filter
  derived/indices/notation.json     every declared symbol (definitions + objects) with
                                    what it denotes — the anti-notation-drift index the
                                    assembler injects into every bundle
  derived/indices/dependencies.json the dependency graph both ways: statement ->
                                    proofs, statement -> what its proofs use, and the
                                    reverse (id -> the statements whose proofs use it)
                                    — the single-source `proves`/`uses`/`verifies`
                                    fields made queryable from either side; see
                                    build_dependencies on why canon never stores the
                                    reverse edge
  derived/indices/unintegrated.json entity id -> its unintegrated extraction blocks
                                    (tools.provenance) — the integration pass's work
                                    queue, derived by scanning bodies so partial
                                    integration can never lose track of what remains

Capsules (derived/capsules/<id>.md) are deliberately NOT built here: an LLM writes
them, so they are not a deterministic function of canon and can't ride this step's
regenerate-and-byte-diff drift guard. They live in tools.capsules with a hash-based
freshness gate instead (`make build-capsules` / `make check-capsules`).

CI runs this and fails if the committed deterministic artifacts differ from a fresh
build, so those views can never silently drift from the canon sources.
"""
from __future__ import annotations

import json
import sys

from schemas.base import canon_weight

from .common import DERIVED_DIR, INDICES_DIR
from .expositions import MENTION_RE, load_expositions
from .graph import build_graph
from .loader import load_canon
from .provenance import parse_blocks


def _write_json(path, data) -> None:
    # sort_keys is the belt to the emitters' suspenders: every emitter already sorts
    # its lists by hand, but sorting dict keys here makes byte-determinism a property
    # of the writer rather than a convention each emitter has to remember. Without it
    # the derived-drift gate (CI byte-diffs these files) would false-positive the first
    # time an emitter builds a plain, unordered dict.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )


def emit_graph(canon) -> None:
    g = build_graph(canon)
    data = {
        "nodes": [{"id": n, **g.nodes[n]} for n in sorted(g.nodes)],
        "edges": sorted(
            ({"source": u, "target": v, "label": d.get("label")}
             for u, v, d in g.edges(data=True)),
            key=lambda e: (e["source"], e["label"], e["target"]),
        ),
    }
    _write_json(DERIVED_DIR / "graph.json", data)


def emit_relations_index(canon) -> None:
    g = build_graph(canon)
    index = {}
    for n in sorted(g.nodes):
        out_edges = [{"target": v, "label": d.get("label")} for _, v, d in g.out_edges(n, data=True)]
        in_edges = [{"source": u, "label": d.get("label")} for u, _, d in g.in_edges(n, data=True)]
        index[n] = {"out": out_edges, "in": in_edges}
    _write_json(INDICES_DIR / "relations.json", index)


def build_mentions(canon) -> dict[str, list[str]]:
    """entity id -> who (which other entity ids) mentions it in canon prose.

    A pure function of `canon` (mirrors build_appearances/build_canonicity below), kept
    separate from the writer for the same reason those are: tools.impact needs this
    exact reverse-mentions view for its "entities reverse-mentioning it" report and
    should call it directly rather than re-parsing canon bodies a second time or
    depending on `derived/indices/mentions.json` already being fresh on disk.
    """
    mentions: dict[str, list[str]] = {}      # entity -> who mentions it (in prose)
    for eid, body in canon.bodies.items():
        for target in sorted(set(MENTION_RE.findall(body))):
            mentions.setdefault(target, [])
            if eid not in mentions[target]:
                mentions[target].append(eid)
    return mentions


def emit_mentions_index(canon) -> None:
    _write_json(INDICES_DIR / "mentions.json", build_mentions(canon))


def build_appearances(expositions) -> dict[str, list[str]]:
    """entity-id -> sorted list of exposition paths that wiki-link it.

    The Layer-2 analogue of the mentions index: where mentions.json reverses
    canon->canon links, this reverses exposition->canon links, powering retrieval
    ("every writeup touching this statement") and impact analysis. A pure function of
    `expositions` (kept separate from the writer so it is directly testable for
    determinism). Includes any linked id as written; whether an id actually resolves is
    the validator's job, not the index's.
    """
    appearances: dict[str, list[str]] = {}
    for exp in expositions:
        for target in exp.mentions:
            appearances.setdefault(target, [])
            if exp.path not in appearances[target]:
                appearances[target].append(exp.path)
    # sort keys and each path list so the serialized artifact is byte-stable
    return {eid: sorted(paths) for eid, paths in sorted(appearances.items())}


def emit_appearances_index(expositions) -> None:
    _write_json(INDICES_DIR / "appearances.json", build_appearances(expositions))


def build_tags(expositions) -> dict[str, list[str]]:
    """material tag -> sorted list of exposition paths carrying it.

    The retrieval payoff of the tag vocabulary (tools.taxonomy): "every computation
    writeup" in one lookup, the same reverse-index shape as appearances/mentions. A
    pure function of `expositions` (separate from the writer, like its siblings, so
    it's directly testable for content and determinism). Untagged expositions
    contribute nothing; an empty corpus yields ``{}``.
    """
    tags: dict[str, list[str]] = {}
    for exp in expositions:
        for tag in exp.tags:
            tags.setdefault(tag, [])
            if exp.path not in tags[tag]:
                tags[tag].append(exp.path)
    return {tag: sorted(paths) for tag, paths in sorted(tags.items())}


def emit_tags_index(expositions) -> None:
    _write_json(INDICES_DIR / "tags.json", build_tags(expositions))


def build_unintegrated(canon) -> dict[str, list[dict]]:
    """entity id -> its unintegrated extraction blocks, in body order.

    The integration pass's durable work queue: a block's presence means an extraction
    landed facts that no one has woven into settled prose yet. Derived by scanning
    bodies for the tools.provenance markers rather than remembered from apply time, so
    deleting a block (integrating it) is the single, sufficient act that clears the
    queue entry. A pure function of `canon` (mirrors its sibling builders) for direct
    testability; per-block note counts let a triager size the job without opening the
    file.
    """
    out: dict[str, list[dict]] = {}
    for eid in sorted(canon.bodies):
        blocks = parse_blocks(canon.bodies[eid])
        if blocks:
            out[eid] = [
                {"date": b.date, "source": b.source, "plan": b.plan,
                 "notes": len(b.notes),
                 "spoiler_notes": sum(1 for n in b.notes if n.spoiler)}
                for b in blocks
            ]
    return out


def emit_unintegrated_index(canon) -> None:
    _write_json(INDICES_DIR / "unintegrated.json", build_unintegrated(canon))


def build_canonicity(canon) -> dict:
    """id -> {state, weight} for every entity, sorted for byte-stable serialization.

    A pure function of `canon` (kept separate from the writer, like build_appearances
    above, so it is directly testable for content and determinism). Tombstones are
    included like any other entity: their `canon_state` still means what it says even
    though `merged_into` also routes references away from them.
    """
    return {
        eid: {"state": e.canon_state, "weight": canon_weight(e)}
        for eid, e in sorted(canon.entities.items())
    }


def emit_canonicity_index(canon) -> None:
    _write_json(INDICES_DIR / "canonicity.json", build_canonicity(canon))


def build_notation(canon) -> list[dict]:
    """Every declared symbol, sorted by (symbol, entity) for byte-stable output.

    The anti-notation-drift index (.docs/PLAN.md pain point 1): definitions and
    objects carry `notation:` records (schemas.base.Notation), and this flattens them
    into one list the assembler injects into every context bundle — so an agent coining
    a symbol sees what is already taken, and an agent reading one can resolve it.
    A pure function of `canon` (mirrors its sibling builders); tombstones contribute
    nothing (their notation retired with them).
    """
    rows: list[dict] = []
    for eid, e in sorted(canon.entities.items()):
        if e.merged_into is not None:
            continue
        for n in getattr(e, "notation", []) or []:
            row = {"symbol": n.symbol, "denotes": n.denotes, "entity": eid}
            if n.notes:
                row["notes"] = n.notes
            rows.append(row)
    return sorted(rows, key=lambda r: (r["symbol"], r["entity"]))


def emit_notation_index(canon) -> None:
    _write_json(INDICES_DIR / "notation.json", build_notation(canon))


def build_dependencies(canon) -> dict:
    """Both directions of the dependency graph, from the typed fields that own it.

    Canon states each dependency fact exactly once: a proof owns `proves` and `uses`,
    a computation owns `verifies` and `depends_on`, a statement or definition owns
    `invokes` (what its formulation is stated in terms of) — and no entity carries
    reverse fields (a hand-maintained mirror would be the same fact written twice,
    with reciprocity churn on every edit; tools.validate.check_field_shadowing
    refuses relation-shaped rewrites of the fields). This index is what makes the
    other directions first-class anyway:

      proofs      statement id -> the proof entities that prove it
      uses        statement id -> everything its proofs use (union, sorted)
      used_by     id -> the statements whose proofs use it (the reverse of `uses`)
      verified_by statement id -> the computations that verify it
      invoked_by  id -> the statements/definitions whose formulation invokes it

    A pure function of `canon` (mirrors its sibling builders) for direct testability;
    entities with no entry are omitted rather than mapped to [], matching
    mentions/appearances.
    """
    proofs: dict[str, list[str]] = {}
    uses: dict[str, list[str]] = {}
    used_by: dict[str, list[str]] = {}
    verified_by: dict[str, list[str]] = {}
    invoked_by: dict[str, list[str]] = {}

    for pid in sorted(canon.entities):
        p = canon.entities[pid]
        if p.type == "proof" and p.merged_into is None:
            proofs.setdefault(p.proves, []).append(pid)
            for u in sorted(set(p.uses)):
                uses.setdefault(p.proves, [])
                if u not in uses[p.proves]:
                    uses[p.proves].append(u)
                used_by.setdefault(u, [])
                if p.proves not in used_by[u]:
                    used_by[u].append(p.proves)
        elif p.type == "computation" and p.merged_into is None:
            for s in sorted(set(p.verifies)):
                verified_by.setdefault(s, []).append(pid)
        elif p.type in ("statement", "definition") and p.merged_into is None:
            for target in sorted(set(p.invokes)):
                invoked_by.setdefault(target, []).append(pid)

    return {
        "proofs": {k: sorted(v) for k, v in sorted(proofs.items())},
        "used_by": {k: sorted(v) for k, v in sorted(used_by.items())},
        "uses": {k: sorted(v) for k, v in sorted(uses.items())},
        "verified_by": {k: sorted(v) for k, v in sorted(verified_by.items())},
        "invoked_by": {k: sorted(v) for k, v in sorted(invoked_by.items())},
    }


def emit_dependencies_index(canon) -> None:
    _write_json(INDICES_DIR / "dependencies.json", build_dependencies(canon))


def build() -> int:
    canon = load_canon()
    if canon.errors:
        print("Refusing to build: canon has load/validation errors. Run validate first.")
        for path, msg in canon.errors:
            print(f"  {path}: {msg}")
        return 1
    expositions = load_expositions()
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    emit_graph(canon)
    emit_relations_index(canon)
    emit_mentions_index(canon)
    emit_appearances_index(expositions)
    emit_tags_index(expositions)
    emit_canonicity_index(canon)
    emit_notation_index(canon)
    emit_dependencies_index(canon)
    emit_unintegrated_index(canon)
    # Capsules are handled out-of-band by tools.capsules (LLM-written; not
    # deterministic). See this module's docstring and `make check-capsules`.
    print(f"Built deterministic derived/ from {len(canon.entities)} entities "
          f"and {len(expositions)} exposition(s).")
    return 0


if __name__ == "__main__":
    sys.exit(build())

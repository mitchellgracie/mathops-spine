"""Assemble a minimal, bounded context bundle for an agent working on one entity.

    python -m tools.assemble_context thm.main_bound --k 1
    python -m tools.assemble_context thm.main_bound --k 2 --budget 1500
    python -m tools.assemble_context thm.main_bound --k 1 --full-neighbors
    python -m tools.assemble_context thm.main_bound --k 2 --max-canon-weight 1

The bundle is: the TARGET's full source file (the thing the agent edits) + the notation
index (always — see below) + its k-hop neighbours as terse capsules (multi-resolution:
you pay full resolution only for the entity under edit).

Because proofs are entities (settled decision: theorems are not their proofs), this
shape does the right thing for free: a statement's 1-hop neighbourhood contains its
proof entities' capsules — the strategy and dependency footprint — without the proof
*text*, which is pulled only by targeting the proof id itself or passing
--full-neighbors.

The notation index (derived from every definition's/object's `notation:` records) is
injected into every bundle, right after the target and before any neighbour, so it
survives budget trimming: inheriting the project's symbols instead of coining new ones
is the whole anti-notation-drift mechanism (.docs/PLAN.md pain point 1), and the index
is small by construction.

Cost scales with the active working set, not the size of the knowledge base. A token
estimate is printed so the economy is visible (approx: chars/4 — swap in a real
tokenizer for exact counts; e.g. Anthropic's count-tokens endpoint).

`--max-canon-weight N` (see schemas.base.canon_weight / CANON_STATES) lets an agent ask
for only the more-authoritative layers of canon — e.g. excluding `apocryphal` sandbox
material or `deprecated` superseded formulations from a working set. The filter applies
to *neighbours only*: the target is what the caller asked to work on, so it is always
included even if it exceeds the cap (a notice is printed instead of silently returning
nothing) — filtering the very entity under edit out from under the caller would defeat
the assembler's purpose.
"""
from __future__ import annotations

import argparse
import sys

from schemas.base import canon_weight

from .build import build_notation
from .capsules import capsule_body
from .common import CANON_DIR
from .graph import build_graph, neighborhood
from .loader import load_canon


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _notation_block(canon) -> str | None:
    """The whole notation index as one terse block, or None when nothing is declared."""
    rows = build_notation(canon)
    if not rows:
        return None
    lines = [f"{r['symbol']}  —  {r['denotes']}  [{r['entity']}]" for r in rows]
    return "# --- notation index (project-wide; use these, don't coin) ---\n" + \
        "\n".join(lines) + "\n"


def assemble(target: str, k: int, full_neighbors: bool, budget: int | None,
            max_canon_weight: int | None = None, canon_dir=CANON_DIR) -> str:
    canon = load_canon(canon_dir)
    if target not in canon.entities:
        raise SystemExit(f"unknown entity: {target!r}")

    g = build_graph(canon)
    dist = neighborhood(g, target, k)              # {id: hop}
    neighbors = sorted((d, eid) for eid, d in dist.items() if eid != target)

    blocks: list[tuple[str, str]] = []             # (label, text)

    # 1) target: the real source file the agent will edit. Always blocks[0] — the budget
    # trimming below keeps blocks[0] unconditionally, and --max-canon-weight must not
    # remove it either (see module docstring): if it would have, append a visible notice
    # right after it instead of silently returning a bundle short of what was asked for.
    target_entity = canon.entities[target]
    target_src = canon.paths[target].read_text()
    blocks.append((f"TARGET {target} (full source)",
                   f"# === TARGET: {target} ===\n{target_src.rstrip()}\n"))
    if max_canon_weight is not None and canon_weight(target_entity) > max_canon_weight:
        blocks.append(("canon-weight notice",
                       f"# [note: TARGET {target} has canon_state "
                       f"{target_entity.canon_state!r} (weight {canon_weight(target_entity)}), "
                       f"above --max-canon-weight {max_canon_weight}; included anyway — "
                       f"the filter applies to neighbours only]\n"))

    # 2) the notation index, before any neighbour, so budget trimming (which keeps
    # blocks in order until the budget runs out) preserves it ahead of far context.
    notation = _notation_block(canon)
    if notation is not None:
        blocks.append(("notation index", notation))

    # 2b) the neighbour-side half of the canon-weight filter: drop neighbours whose
    # canon_state weight exceeds the cap. Nearest-first ordering is preserved.
    if max_canon_weight is not None:
        neighbors = [(hop, eid) for hop, eid in neighbors
                    if canon_weight(canon.entities[eid]) <= max_canon_weight]

    # 3) neighbours, nearest first, as capsules (or full files if requested)
    for hop, eid in neighbors:
        ent = canon.entities[eid]
        if full_neighbors:
            text = canon.paths[eid].read_text().rstrip()
        else:
            text = capsule_body(eid, ent).rstrip()
        blocks.append((f"{eid} (hop {hop})",
                       f"# --- context: {eid} [{ent.type}, hop {hop}] ---\n{text}\n"))

    # apply a token budget: keep the target, then add blocks until the budget is hit
    if budget is not None:
        kept = [blocks[0]]
        used = est_tokens(blocks[0][1])
        dropped = 0
        for label, text in blocks[1:]:
            t = est_tokens(text)
            if used + t > budget:
                dropped += 1
                continue
            kept.append((label, text))
            used += t
        if dropped:
            kept.append(("budget", f"# [budget {budget} tok: dropped {dropped} "
                                   f"lower-priority block(s)]\n"))
        blocks = kept

    bundle = "\n".join(text for _, text in blocks)
    return bundle


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble a context bundle for one entity.")
    ap.add_argument("entity", help="entity id, e.g. thm.main_bound")
    ap.add_argument("--k", type=int, default=1, help="neighbourhood radius in hops (default 1)")
    ap.add_argument("--full-neighbors", action="store_true",
                    help="include neighbours' full source instead of capsules")
    ap.add_argument("--budget", type=int, default=None,
                    help="approx token budget; drops far blocks first")
    ap.add_argument("--max-canon-weight", type=int, default=None,
                    help="drop neighbours above this canon_weight (core=0..deprecated=3); "
                         "default: include every tier. Never filters the target.")
    ap.add_argument("--stats", action="store_true", help="print size stats to stderr")
    args = ap.parse_args()

    bundle = assemble(args.entity, args.k, args.full_neighbors, args.budget,
                      args.max_canon_weight)
    sys.stdout.write(bundle)
    if args.stats:
        sys.stderr.write(
            f"\n[~{est_tokens(bundle)} tokens, {len(bundle)} chars, "
            f"k={args.k}, full_neighbors={args.full_neighbors}]\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

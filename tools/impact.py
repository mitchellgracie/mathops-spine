"""Reverse-mentions impact analysis. Run as: python -m tools.impact [--base <ref>]

Given a diff against a base ref, answers the impact-analysis promise: *which
expositions and entities are affected by the entities this change touched?* ("the main
bound's hypotheses changed — flag the 4 writeups citing it.")

This is the **deterministic** half of the continuity-check pipeline; the LLM
canon-check (tools.canon_check) consumes this module's output as its bounded context,
but this module itself never calls a model and never fails the gate — it is advisory,
informational, exit 0 always.

**What it does NOT duplicate.** Diff classification (new/changed/removed entity ids
from a `git diff` against a base ref) is `tools.pr_annotate`'s job already — this
module imports that machinery rather than re-parsing `git diff --name-status` a second
time. What *is* new here: (a) looking up the reverse-mentions views
(`tools.build.build_appearances` / `build_mentions`, called directly as pure functions
of the *current* canon+expositions, not read back off a possibly-stale `derived/` file
— impact analysis wants to know what NOW references the changed entity, not what
referenced it at the base ref) and (b) comparing a changed/removed entity's
claim-bearing fields at the base ref against its current (or absent, if removed)
values, which requires reading the base ref's frontmatter (`git show <base>:<path>`,
the same primitive `tools.pr_annotate._entity_id_at` already uses for deleted files)
and re-validating it through the real Pydantic model so the comparison is type-aware.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import frontmatter

from schemas.registry import model_for_type

from .build import build_appearances, build_mentions
from .common import ROOT
from .expositions import load_expositions
from .loader import Canon, load_canon
from .pr_annotate import BaseRefError, DiffSummary, git_diff_name_status, summarize_diff

# Per-type allowlist of *claim-bearing* fields: the fields whose change means the
# entity now asserts something different (as opposed to cosmetic/organizational fields
# like aliases or tags). A change touching one of these earns the CONTINUITY REVIEW
# flag in the report; the LLM canon-check leans on the same signal. Owned here (its
# only consumer) — promote to schemas/ only if a second module needs it.
COMPARABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "definition": ("statement", "notation"),
    "statement": ("statement", "status", "kind", "refuted_by"),
    "proof": ("proves", "uses", "completeness", "strategy"),
    "object": ("instance_of", "notation"),
    "source": ("authors", "year", "bibkey"),
    "computation": ("verifies", "covers", "conclusion", "path"),
    "technique": ("related",),
}


def _metadata_at(path: str, ref: str, *, root: Path = ROOT) -> dict | None:
    """Best-effort full frontmatter metadata of `path` at git ref `ref`.

    None on any failure (path didn't exist at that ref, malformed YAML, ...). This is
    `tools.pr_annotate._entity_id_at`'s sibling: that helper only needs the `id` field
    to report *what* was removed; claim-field comparison needs every comparable field,
    so this returns the whole metadata dict instead of duplicating the git-show-and-parse
    plumbing for just one key.
    """
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root,
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        post = frontmatter.loads(proc.stdout)
    except Exception:
        return None
    return dict(post.metadata)


def _model_at(entity_type: str | None, metadata: dict | None):
    """Best-effort typed model instance from raw frontmatter metadata, or None.

    Re-validating through the real Pydantic model (rather than comparing raw YAML
    values) means both sides of the comparison normalize the same way regardless of how
    that revision happened to spell a value. None covers every reason this can't happen
    (unknown type, a schema-version mismatch against a much older revision,
    extra/missing fields) — the affected fields then read as "no claim data on this
    side" rather than crashing; impact is advisory, not a second validator, so a
    best-effort miss here is acceptable.
    """
    if entity_type is None or metadata is None:
        return None
    model = model_for_type(entity_type)
    if model is None:
        return None
    try:
        return model(**metadata)
    except Exception:
        return None


def _value(entity, field: str):
    """A comparable projection of one field's value, or None if unset.

    Lists compare order-independently; embedded models (Notation records) compare by
    their dumped dict form so two loads of the same YAML are equal.
    """
    value = getattr(entity, field, None)
    if value is None:
        return None
    if isinstance(value, list):
        return sorted(
            (json.dumps(v.model_dump(mode="json"), sort_keys=True)
             if hasattr(v, "model_dump") else v)
            for v in value
        )
    return value


def claim_fields_changed(entity_type: str, base_entity, current_entity) -> list[str]:
    """Which of `entity_type`'s claim-bearing fields differ between two model instances.

    Either side may be None (removed entity: no current model; brand-new base metadata
    that failed to validate: no base model) — `_value` degrades to None for a missing
    side via `getattr`'s default, so "no claim data" compares as no different from "field
    genuinely unset," never as a spurious change.
    """
    return sorted(
        f for f in COMPARABLE_FIELDS.get(entity_type, ())
        if _value(base_entity, f) != _value(current_entity, f)
    )


def build_impact(
    base: str,
    *,
    root: Path = ROOT,
    canon: Canon | None = None,
    summary: DiffSummary | None = None,
) -> dict[str, dict[str, list[str]]]:
    """entity id -> {expositions, mentioned_by, claim_fields_changed} for every changed
    or removed entity in the diff against `base`.

    New entities are deliberately excluded (see module docstring: this reports the
    consequences of a *mutation*, not the arrival of a brand-new entity nothing could
    have referenced yet).

    `canon` and `summary` are both injectable so `tools.pr_annotate.annotate` (which
    already loaded canon and computed the diff summary for its own report) doesn't pay
    to redo either — mirrors `summarize_diff`'s own `canon` parameter.
    """
    canon = load_canon(root / "canon") if canon is None else canon
    summary = summarize_diff(base, root=root, canon=canon) if summary is None else summary

    appearances = build_appearances(load_expositions(root / "expositions"))
    mentions = build_mentions(canon)

    # Rename-aware base-path resolution: a changed file whose git status is R/C reports
    # its *new* path in DiffSummary (see pr_annotate.summarize_diff), but the base ref's
    # content lives at the old path — reuse pr_annotate's own diff-entry parser (not a
    # second `git diff --name-status` implementation) to recover it.
    old_path_of = {e.path: e.old_path for e in git_diff_name_status(base, root=root) if e.old_path}

    impact: dict[str, dict[str, list[str]]] = {}

    for eid in summary.changed_ids:
        entity = canon.entities.get(eid)
        if entity is None:
            continue
        current_relpath = str(canon.paths[eid].relative_to(root))
        base_path = old_path_of.get(current_relpath, current_relpath)
        base_entity = _model_at(entity.type, _metadata_at(base_path, base, root=root))
        impact[eid] = {
            "expositions": sorted(appearances.get(eid, [])),
            "mentioned_by": sorted(mentions.get(eid, [])),
            "claim_fields_changed": claim_fields_changed(entity.type, base_entity, entity),
        }

    for path, eid in summary.removed:
        if eid is None:
            continue
        base_metadata = _metadata_at(path, base, root=root)
        entity_type = base_metadata.get("type") if base_metadata else None
        base_entity = _model_at(entity_type, base_metadata)
        impact[eid] = {
            "expositions": sorted(appearances.get(eid, [])),
            "mentioned_by": sorted(mentions.get(eid, [])),
            "claim_fields_changed": claim_fields_changed(entity_type or "", base_entity, None),
        }

    return dict(sorted(impact.items()))


def render_markdown(base: str, impact: dict[str, dict[str, list[str]]]) -> str:
    lines = [f"## Impact (base: `{base}`)", ""]

    if not impact:
        lines.append("No canon changes detected against this base.")
        return "\n".join(lines) + "\n"

    flagged = sorted(eid for eid, d in impact.items() if d["claim_fields_changed"])
    lines.append(
        f"**{len(impact)} changed/removed entit{'y' if len(impact) == 1 else 'ies'} "
        f"analyzed ({len(flagged)} flagged for continuity review).**"
    )
    lines.append("")

    for eid, d in impact.items():
        tag = " — **CONTINUITY REVIEW**" if d["claim_fields_changed"] else ""
        lines.append(f"- `{eid}`{tag}")
        if d["claim_fields_changed"]:
            lines.append(f"  - claim fields changed: {', '.join(d['claim_fields_changed'])}")
        if d["expositions"]:
            lines.append(f"  - expositions ({len(d['expositions'])}): "
                          + ", ".join(f"`{s}`" for s in d["expositions"]))
        if d["mentioned_by"]:
            lines.append(f"  - mentioned by ({len(d['mentioned_by'])}): "
                          + ", ".join(f"`{m}`" for m in d["mentioned_by"]))
        if not (d["expositions"] or d["mentioned_by"] or d["claim_fields_changed"]):
            lines.append("  - nothing currently references this entity")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reverse-mentions impact analysis: which expositions/entities are "
                     "affected by a diff's changed or removed entities.")
    parser.add_argument("--base", default="origin/main",
                        help="ref to diff canon/ against (default: origin/main)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON instead of Markdown")
    args = parser.parse_args(argv)

    try:
        impact = build_impact(args.base)
    except BaseRefError as exc:
        # A bad ref is a usage error, not a "finding" — this is the one way tools.impact
        # exits non-zero; see the module docstring's "advisory, exit 0 always" for the
        # rest (which is about findings, not invocation failures), and
        # tools.pr_annotate.main's identical treatment of the same exception.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(impact, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_markdown(args.base, impact))
    return 0


if __name__ == "__main__":
    sys.exit(main())

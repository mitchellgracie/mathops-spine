"""Controlled vocabulary for *material tags* — the topical labels carried by raw
material and threaded downstream onto the plan it produces and the exposition it emits.

Why a curated vocabulary rather than free-form strings: tags are only useful if they
*collide* — "every computation discussion" is a query worth having only when everyone
spells it ``computation`` and not ``computations`` / ``case-check`` / ``verification``.
So this module is the one place the allowed set lives, and both writers of tags
(``tools.extract`` on raw and plans) and readers of them (``tools.expositions`` on
exposition files, ``tools.build`` on the index) share it. Unknown tags are deliberately
a *warning*, never a hard error — the pipeline must not refuse an author who coins a
new theme mid-thought — but the warning nudges the vocabulary back toward convergence,
and adding a real new tag is a one-line edit here.

These label *what a piece of material is about*, orthogonal to ``kind`` (which is a
pipeline-behaviour switch: does this emit an exposition?) and to entity *type*
(def/thm/…). A single paper writeup can be tagged ``literature`` and ``technique`` at
once.

BOOTSTRAP NOTE: the set below is structural (kinds of mathematical material), because
the repo is project-agnostic until seeded. The research project's own subfield tags
("moduli", "p-adic", whatever the work actually splits along) belong here too — add
them at WP5 seeding, when real material shows what the useful splits are.
"""
from __future__ import annotations

import re

# tag -> one-line gloss of what belongs under it. Keep the keys single, lowercase,
# hyphen-joined tokens (see `normalize_tags`); keep the set small and orthogonal —
# a tag earns its place by being something you'd actually want to filter a pile of
# material down to. Document longhand-to-tag mappings where authors write tags
# (raw/_TEMPLATE.md), not by multiplying near-synonyms here.
MATERIAL_TAGS: dict[str, str] = {
    "literature": "writeups of published papers/books — imported statements and proofs",
    "background": "known material recorded for the project's foundation, not its frontier",
    "mainline": "the project's own frontier — new statements, proof attempts, strategy",
    "conjecture": "open questions, speculation, plausibility arguments",
    "computation": "case checks, experiments, numerical/symbolic evidence",
    "examples": "worked examples and counterexamples",
    "technique": "methods and machinery — how proofs go, transferable tricks",
    "notation": "conventions, symbols, terminology decisions",
    "writeup": "prose bound for a paper/survey — exposition-quality drafts",
}

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def normalize_tags(value) -> list[str]:
    """Coerce a raw ``tags:`` value into a clean, sorted, de-duplicated list.

    Accepts the shapes authors actually write — a missing key (``None``), a single
    string, or a list — and canonicalizes each entry to lowercase hyphen-joined tokens
    (``"Case Checks" -> "case-checks"``) so equality is spelling-robust. Sorted output
    keeps every downstream serialization (plan meta, exposition frontmatter, the
    derived index) byte-stable without each caller re-sorting.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: set[str] = set()
    for item in items:
        tag = _TOKEN_RE.sub("-", str(item).strip().lower()).strip("-")
        if tag:
            out.add(tag)
    return sorted(out)


def unknown_tags(tags) -> list[str]:
    """The normalized tags not in `MATERIAL_TAGS`, sorted — advisory, never fatal.

    Callers surface these as warnings so the vocabulary converges over time; they must
    not block a plan or an apply on them (a fresh theme is legitimate — promote it into
    `MATERIAL_TAGS` when it recurs).
    """
    return [t for t in normalize_tags(tags) if t not in MATERIAL_TAGS]

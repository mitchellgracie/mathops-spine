"""Unintegrated-provenance blocks: extraction's paper trail inside entity bodies.

When an applied extraction plan carries schema-less prose facts (``add.notes`` on an
update finding — see tools.extract), apply appends them to the target entity's body as
a *stamped block* rather than weaving them into the prose. The stamp records where the
facts came from (date, archived raw source, approved plan) and — by existing at all —
flags that a human/Lore-Keeper integration pass is still owed: settled canon prose
never carries these markers. Compaction is that pass: rewrite the body so the facts
read as canon, then delete the whole block. The queue of pending blocks is derived, not
remembered (``derived/indices/unintegrated.json``; tools.build scans for markers), so
partial integration can never silently lose track of what remains.

The format is an HTML-comment pair because it must survive three consumers untouched:
tools.fmt (which canonicalizes frontmatter but passes bodies through verbatim), any
Markdown renderer (comments are invisible in rendered prose), and a human reading the
file (the marker text says exactly what the block is). Notes render as a bullet list —
visually distinct from settled prose, one claim per bullet. A leading ``**(spoiler)**``
tag is *advisory provenance only* — a vestige of the ancestor spine's knowledge-gating
layer, still accepted so plans that carry it parse: it records that the author
considered the fact unrevealed as of extraction. Nothing consumes it mechanically — integration weaves
a tagged fact into settled prose exactly like any other and drops the tag with the
block; the approved plans in ``extraction/approved/`` remain the running inventory.

This module is the single owner of the format: extract renders blocks, build/validate
parse them, capsules strips them (an unintegrated block is workflow state, not settled
semantic content, so it is invisible to the capsule freshness hash and the capsule
generator — a capsule stays fresh across an append and goes stale at compaction, when
the body's real prose changes).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# One block = open marker, bullet-list notes, close marker. The open marker's key=value
# fields are fixed and ordered so blocks are byte-deterministic (the derived-drift
# philosophy applied to canon-embedded workflow state); `status=unintegrated` is literal
# text, not a variable — a block never flips to "integrated", it is deleted.
_OPEN_RE = re.compile(
    r"<!--\s*extraction:\s*date=(?P<date>\S+)\s+source=(?P<source>\S+)\s+"
    r"plan=(?P<plan>\S+)\s+status=unintegrated\s*-->"
)
CLOSE_MARKER = "<!-- /extraction -->"
_BLOCK_RE = re.compile(_OPEN_RE.pattern + r"\n(?P<notes>.*?)" + re.escape(CLOSE_MARKER),
                       re.S)
# Anything that *looks like* it wants to be one of our markers but doesn't parse as a
# well-formed block. Caught by integrity_problems so a typo'd stamp fails the gate
# instead of silently dropping out of the unintegrated index.
_MARKER_LIKE_RE = re.compile(r"<!--\s*/?extraction[:\s]")

SPOILER_TAG = "**(spoiler)**"


@dataclass(frozen=True)
class Note:
    """One net-new fact awaiting integration. `spoiler` mirrors the plan's marking."""

    text: str
    spoiler: bool = False


@dataclass(frozen=True)
class Block:
    """One parsed unintegrated block: its provenance stamp plus the notes it carries."""

    date: str      # YYYY-MM-DD, the day the plan was applied
    source: str    # repo-relative archived raw file (raw/extracted/...)
    plan: str      # repo-relative approved plan (extraction/approved/...)
    notes: list[Note] = field(default_factory=list)


def stamp_date(now: datetime | None = None) -> str:
    """The stamp's date field; injectable ``now`` for deterministic tests."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def render_block(notes: list[Note], *, date: str, source: str, plan: str) -> str:
    """One block's full text, newline-terminated. Deterministic for identical input."""
    lines = [f"<!-- extraction: date={date} source={source} plan={plan} "
             f"status=unintegrated -->"]
    for n in notes:
        lines.append(f"- {SPOILER_TAG} {n.text}" if n.spoiler else f"- {n.text}")
    lines.append(CLOSE_MARKER)
    return "\n".join(lines) + "\n"


def append_block(body: str, block_text: str) -> str:
    """Append a rendered block to a body, separated by one blank line."""
    body = body.rstrip("\n")
    return f"{body}\n\n{block_text}" if body else block_text


def _parse_notes(notes_text: str) -> list[Note]:
    """Bullet lines -> Notes; a non-bullet line continues the previous note.

    Continuation handling matters because a note is an arbitrary YAML string: a long
    fact may have been reflowed by an editor. Content before the first bullet (there
    should be none) is ignored rather than invented into a note.
    """
    notes: list[Note] = []
    for line in notes_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            text = stripped[2:].strip()
            spoiler = text.startswith(SPOILER_TAG)
            if spoiler:
                text = text[len(SPOILER_TAG):].strip()
            notes.append(Note(text=text, spoiler=spoiler))
        elif notes:
            notes[-1] = Note(text=f"{notes[-1].text} {stripped}", spoiler=notes[-1].spoiler)
    return notes


def parse_blocks(body: str) -> list[Block]:
    """Every well-formed unintegrated block in a body, in file order.

    Malformed marker debris is deliberately NOT returned as a block — it is
    `integrity_problems`' finding. Parsing and integrity are split so the index/build
    path (which wants blocks) and the gate path (which wants errors) each read the one
    thing they act on.
    """
    return [
        Block(
            date=m.group("date"), source=m.group("source"), plan=m.group("plan"),
            notes=_parse_notes(m.group("notes")),
        )
        for m in _BLOCK_RE.finditer(body or "")
    ]


def integrity_problems(body: str) -> list[str]:
    """Marker debris a well-formed body must not contain (unpaired/malformed markers).

    Works by elimination: strip every well-formed block, then anything left that still
    looks like an extraction marker is a problem. This catches an open without a close,
    a stray close, and a typo'd stamp line in one sweep, without needing to guess what
    the author meant.
    """
    leftover = _BLOCK_RE.sub("", body or "")
    problems = []
    for line in leftover.splitlines():
        if _MARKER_LIKE_RE.search(line):
            problems.append(
                f"malformed or unpaired extraction marker: {line.strip()!r} "
                f"(a block is `<!-- extraction: date=... source=... plan=... "
                f"status=unintegrated -->` ... `{CLOSE_MARKER}`)"
            )
    return problems


def strip_blocks(body: str) -> str:
    """The body with every well-formed unintegrated block removed.

    This is the projection tools.capsules hashes and generates from: identical to the
    input byte-for-byte when no blocks are present (so no existing capsule hash moves),
    and stable across block appends (so a capsule stays fresh until compaction rewrites
    the real prose). Whitespace left behind by a removed block is collapsed so an
    append-then-compact round trip can restore the original bytes.
    """
    if not body or "<!-- extraction:" not in body:
        return body
    stripped = _BLOCK_RE.sub("", body)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.rstrip("\n") + "\n" if stripped.strip() else ""

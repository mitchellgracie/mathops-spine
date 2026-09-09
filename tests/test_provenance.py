"""Tests for tools.provenance (unintegrated extraction blocks) and their consumers.

The format has three load-bearing properties, each pinned here: a rendered block
parses back to exactly what was rendered (extract writes -> build/validate read);
`strip_blocks` is byte-neutral on bodies without blocks and removes blocks cleanly
(the capsule freshness hash rides on this — see tools.capsules.render_source); and
marker debris is an integrity problem, never a silently-skipped block (the validator's
`provenance-marker` ERROR rides on that).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import provenance as pv
from tools.build import build_unintegrated
from tools.loader import load_canon
from tools.validate import Report, check_unintegrated

NOTES = [pv.Note("Ana tends the vineyard alone."),
         pv.Note("Ana buried the Hand's seal beneath it.", spoiler=True)]
STAMP = dict(date="2026-07-16", source="raw/extracted/chapter.md",
             plan="extraction/approved/chapter.md")


def test_render_parse_round_trip():
    block = pv.render_block(NOTES, **STAMP)
    parsed = pv.parse_blocks(f"Settled prose.\n\n{block}")
    assert len(parsed) == 1
    b = parsed[0]
    assert (b.date, b.source, b.plan) == tuple(STAMP.values())
    assert b.notes == NOTES


def test_parse_handles_multiple_blocks_and_continuation_lines():
    one = pv.render_block([pv.Note("First fact.")], **STAMP)
    # a note reflowed onto two lines by an editor still parses as one note
    two = pv.render_block([pv.Note("Second fact.")], **{**STAMP, "date": "2026-08-01"})
    two = two.replace("- Second fact.", "- Second fact,\n  continued.")
    blocks = pv.parse_blocks(f"body\n\n{one}\n{two}")
    assert [b.date for b in blocks] == ["2026-07-16", "2026-08-01"]
    assert blocks[1].notes == [pv.Note("Second fact, continued.")]


def test_strip_is_byte_neutral_without_blocks():
    for body in ("", "plain prose\n", "prose with <!-- a comment --> inline\n",
                 "no trailing newline"):
        assert pv.strip_blocks(body) == body


def test_strip_removes_blocks_and_normalizes_whitespace():
    block = pv.render_block(NOTES, **STAMP)
    body = pv.append_block("Settled prose.\n", block)
    assert pv.strip_blocks(body) == "Settled prose.\n"
    # a body that is nothing but a block strips to empty
    assert pv.strip_blocks(pv.append_block("", block)) == ""


def test_append_block_separates_with_one_blank_line():
    block = pv.render_block([pv.Note("A fact.")], **STAMP)
    assert pv.append_block("Prose.", block) == f"Prose.\n\n{block}"
    assert pv.append_block("", block) == block


def test_integrity_flags_debris_but_not_wellformed_blocks():
    good = pv.append_block("Prose.", pv.render_block(NOTES, **STAMP))
    assert pv.integrity_problems(good) == []
    # a stray close, and an open whose stamp is typo'd (missing plan=)
    for debris in (pv.CLOSE_MARKER,
                   "<!-- extraction: date=2026-07-16 source=x status=unintegrated -->"):
        problems = pv.integrity_problems(f"{good}\n{debris}\n")
        assert len(problems) == 1 and "extraction marker" in problems[0]
        assert pv.parse_blocks(f"{good}\n{debris}\n")  # the good block still parses


def test_stamp_date_is_injectable():
    assert pv.stamp_date(datetime(2026, 7, 16, 23, 59, tzinfo=timezone.utc)) == "2026-07-16"


# --- the consumers: validator finding + build index --------------------------------

def _mini_canon(tmp_path, body: str):
    p = tmp_path / "canon" / "statements" / "ana.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\nid: thm.ana\ntype: statement\nname: Ana\n---\n{body}")
    return load_canon(tmp_path / "canon")


def test_check_unintegrated_warns_on_blocks_and_errors_on_debris(tmp_path):
    block = pv.render_block(NOTES, **STAMP)
    canon = _mini_canon(tmp_path, pv.append_block("Prose.", block))
    rep = Report()
    check_unintegrated(canon, rep)
    assert rep.ok()
    assert any(f.code == "unintegrated-notes" and "2 note(s)" in f.message
               for f in rep.findings)

    canon = _mini_canon(tmp_path / "second", f"Prose.\n\n{pv.CLOSE_MARKER}\n")
    rep = Report()
    check_unintegrated(canon, rep)
    assert [f.code for f in rep.findings if f.level == "error"] == ["provenance-marker"]


def test_build_unintegrated_indexes_blocks(tmp_path):
    block = pv.render_block(NOTES, **STAMP)
    canon = _mini_canon(tmp_path, pv.append_block("Prose.", block))
    index = build_unintegrated(canon)
    assert index == {"thm.ana": [{**STAMP, "notes": 2, "spoiler_notes": 1}]}


def test_build_unintegrated_empty_for_clean_canon(tmp_path):
    assert build_unintegrated(_mini_canon(tmp_path, "Prose.\n")) == {}

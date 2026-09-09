"""Tests for the Layer-2 exposition corpus: the reference gate and the appearances
index.

Offline and hermetic, like the rest of the suite: each test writes a tiny canon (and,
where needed, a tiny expositions tree) into a temp dir. Expositions have no frontmatter
schema, so the only invariants worth pinning are (a) a dangling [[id]] in an exposition
is an ERROR and (b) the appearances index is built correctly and is deterministic
(byte-stable across file ordering) so it can ride the CI drift guard.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build import build_appearances
from tools.expositions import Exposition, load_expositions
from tools.loader import load_canon
from tools.validate import Report, check_exposition_references


def write(tmp: Path, relpath: str, body: str) -> None:
    """Write a canon entity file (dedented) under tmp/canon/."""
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


def write_exposition(tmp: Path, collection: str, name: str, body: str) -> None:
    p = tmp / "expositions" / collection / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


THM = """
    ---
    id: thm.a
    type: statement
    name: A
    statement: x
    stated_in: src.paper
    ---
    body
"""
SOURCE = """
    ---
    id: src.paper
    type: source
    name: P
    ---
    body
"""


def _canon(tmp_path: Path):
    write(tmp_path, "statements/a.md", THM)
    write(tmp_path, "sources/paper.md", SOURCE)
    return load_canon(tmp_path / "canon")


# --- exposition reference gate ------------------------------------------------

def test_dangling_exposition_reference_fails_validation(tmp_path):
    canon = _canon(tmp_path)
    expositions = [
        Exposition("expositions/survey/001-ok.md", "We use [[thm.a]] from [[src.paper]]."),
        Exposition("expositions/survey/002-bad.md", "But [[thm.ghost]] never existed."),
    ]
    rep = Report()
    check_exposition_references(canon, rep, expositions=expositions)

    assert not rep.ok()
    assert any("002-bad.md: [[thm.ghost]] does not exist (dangling reference)" in e
               for e in rep.errors), rep.errors
    # the resolvable links in the same batch do not raise
    assert not any("thm.a" in e for e in rep.errors)
    assert not any("src.paper" in e for e in rep.errors)


def test_valid_exposition_references_pass(tmp_path):
    canon = _canon(tmp_path)
    expositions = [Exposition("expositions/survey/001.md", "[[thm.a]] of [[src.paper]].")]
    rep = Report()
    check_exposition_references(canon, rep, expositions=expositions)
    assert rep.ok(), rep.errors


def test_check_exposition_references_loads_tree_when_not_injected(tmp_path):
    # end-to-end through load_expositions: a dangling link on disk is caught
    canon = _canon(tmp_path)
    write_exposition(tmp_path, "survey", "001-bad.md", "[[thm.a]] and [[def.nowhere]].")
    expositions = load_expositions(tmp_path / "expositions")
    rep = Report()
    check_exposition_references(canon, rep, expositions=expositions)
    assert any("def.nowhere" in e for e in rep.errors), rep.errors


# --- appearances index -------------------------------------------------------

def test_appearances_index_maps_entities_to_exposition_paths():
    expositions = [
        Exposition("expositions/survey/001-a.md", "[[thm.a]] and [[def.x]]."),
        Exposition("expositions/survey/002-b.md", "[[thm.a]] alone."),
        Exposition("expositions/survey/003-c.md", "[[def.x]] revisited."),
    ]
    idx = build_appearances(expositions)
    assert idx == {
        "def.x": ["expositions/survey/001-a.md", "expositions/survey/003-c.md"],
        "thm.a": ["expositions/survey/001-a.md", "expositions/survey/002-b.md"],
    }


def test_appearances_index_dedupes_repeat_mentions_in_one_file():
    expositions = [Exposition("expositions/survey/001.md", "[[thm.a]] then [[thm.a]] again.")]
    assert build_appearances(expositions) == {"thm.a": ["expositions/survey/001.md"]}


def test_appearances_index_is_deterministic_across_file_ordering():
    a = Exposition("expositions/survey/002-b.md", "[[def.x]] and [[thm.a]].")
    b = Exposition("expositions/survey/001-a.md", "[[thm.a]].")
    c = Exposition("expositions/survey/003-c.md", "[[def.x]].")

    forward = build_appearances([a, b, c])
    shuffled = build_appearances([c, a, b])

    # identical mapping AND identical serialization regardless of input order
    assert forward == shuffled
    assert json.dumps(forward) == json.dumps(shuffled)
    # keys and each path list are sorted
    assert list(forward) == sorted(forward)
    for paths in forward.values():
        assert paths == sorted(paths)


def test_empty_expositions_tree_yields_empty_index(tmp_path):
    assert load_expositions(tmp_path / "does-not-exist") == []
    assert build_appearances([]) == {}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

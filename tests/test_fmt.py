"""Tests for the canonical formatter (tools.fmt).

The formatter's contract has two halves that these pin: it is idempotent and
byte-stable (a second pass is a no-op), and it is a pure restyle — reordering keys and
normalizing YAML must never change the meaning the loader parses out (so derived stays
untouched). LaTeX statements are the acceptance case the fork's plan named: multi-line
`statement:` fields must survive a fmt round-trip byte-identically as literal block
scalars, whatever backslashes, `%`, alignment environments, or unicode they carry. A
file that cannot parse is left alone.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import fmt
from tools.loader import load_canon


def write(tmp: Path, relpath: str, body: str) -> None:
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


UNFORMATTED = """
    ---
    type: statement
    name: Widget bound
    id: thm.bound
    status: proved
    kind: theorem
    stated_in: src.paper2024
    aliases: [the bound]
    ---
    Discussion of the bound.
"""
SOURCE = """
    ---
    id: src.paper2024
    type: source
    name: Example Paper 2024
    ---
    body
"""

# Deliberately nasty LaTeX: backslashes, %, &, aligned environment, unicode, $$.
LATEX_STATEMENT = (
    "Let $X \\subseteq \\mathbb{P}^n$ be smooth. Then\n"
    "\\begin{aligned}\n"
    "  h^0(X, \\mathcal{O}_X(d)) &\\le \\binom{n+d}{d}, \\\\\n"
    "  h^1(X, \\mathcal{O}_X(d)) &= 0 \\quad \\text{for } d \\gg 0. % Serre\n"
    "\\end{aligned}\n"
    "Kähler assumptions are not needed."
)


def test_format_is_idempotent(tmp_path):
    write(tmp_path, "statements/bound.md", UNFORMATTED)
    canon_dir = tmp_path / "canon"
    assert fmt.run(check=True, canon_dir=canon_dir) == 1   # not yet canonical
    assert fmt.run(check=False, canon_dir=canon_dir) == 0  # format it
    once = (canon_dir / "statements" / "bound.md").read_text()
    assert fmt.run(check=False, canon_dir=canon_dir) == 0  # second pass: no-op
    twice = (canon_dir / "statements" / "bound.md").read_text()
    assert once == twice
    assert fmt.run(check=True, canon_dir=canon_dir) == 0   # now passes --check


def test_format_orders_keys_by_schema_declaration(tmp_path):
    write(tmp_path, "statements/bound.md", UNFORMATTED)
    write(tmp_path, "sources/paper2024.md", SOURCE)
    canon_dir = tmp_path / "canon"
    fmt.run(check=False, canon_dir=canon_dir)
    text = (canon_dir / "statements" / "bound.md").read_text()
    # id, type, name lead (identity fields first in the Entity base).
    keys = [line.split(":", 1)[0] for line in text.splitlines()
            if line and not line.startswith(("-", " ", "#")) and ":" in line]
    assert keys[:3] == ["id", "type", "name"], keys


def test_format_preserves_meaning(tmp_path):
    write(tmp_path, "statements/bound.md", UNFORMATTED)
    write(tmp_path, "sources/paper2024.md", SOURCE)
    canon_dir = tmp_path / "canon"
    before = load_canon(canon_dir).entities["thm.bound"].model_dump(mode="json")
    fmt.run(check=False, canon_dir=canon_dir)
    after = load_canon(canon_dir).entities["thm.bound"].model_dump(mode="json")
    assert before == after


def test_latex_statement_round_trips_byte_identically(tmp_path):
    # The plan's WP1 acceptance test: author a statement as a literal block, fmt once
    # to canonical form, and from then on fmt must be a byte-level no-op while the
    # loaded LaTeX string stays exactly what was authored.
    canon_dir = tmp_path / "canon"
    p = canon_dir / "statements" / "vanish.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    indented = "\n".join("  " + ln if ln else "" for ln in LATEX_STATEMENT.splitlines())
    p.write_text(
        "---\n"
        "id: thm.vanish\n"
        "type: statement\n"
        "name: Vanishing bound\n"
        f"statement: |\n{indented}\n"
        "---\n"
        "body\n"
    )
    loaded_before = load_canon(canon_dir).entities["thm.vanish"].statement
    assert loaded_before.rstrip("\n") == LATEX_STATEMENT

    fmt.run(check=False, canon_dir=canon_dir)
    once = p.read_text()
    # meaning identical after the restyle
    assert load_canon(canon_dir).entities["thm.vanish"].statement.rstrip("\n") == \
        LATEX_STATEMENT
    # canonical form is stable: --check passes and a second pass changes nothing
    assert fmt.run(check=True, canon_dir=canon_dir) == 0
    fmt.run(check=False, canon_dir=canon_dir)
    assert p.read_text() == once
    # and the statement still renders as a readable literal block, not \n escapes
    assert "statement: |" in once
    assert "\\begin{aligned}" in once


def test_multiline_string_emits_literal_block(tmp_path):
    # The str representer: any multi-line frontmatter string prefers `|` style so LaTeX
    # stays human-readable in the file itself.
    text = fmt.canonical_text(
        "statement",
        {"id": "thm.a", "type": "statement", "name": "A",
         "statement": "line one\nline two $\\alpha$"},
        "",
    )
    assert "statement: |" in text or "statement: |-" in text
    assert "line two $\\alpha$" in text


def test_format_leaves_unparseable_file_untouched(tmp_path):
    # Not valid frontmatter: no closing delimiter / junk. Formatter must not mangle it.
    p = tmp_path / "canon" / "statements" / "broken.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    original = "---\nid: thm.b\n: : : not yaml\n"
    p.write_text(original)
    fmt.run(check=False, canon_dir=tmp_path / "canon")
    assert p.read_text() == original


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

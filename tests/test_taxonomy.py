"""Tests for the material-tag vocabulary (tools.taxonomy).

Small by design: the module is a controlled vocabulary plus two coercion helpers. These
pin the contract every caller relies on — normalization is spelling-robust and
deterministic (so plan meta, exposition frontmatter, and the derived index stay
byte-stable), and out-of-vocabulary tags are surfaced but never rejected.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.taxonomy import MATERIAL_TAGS, normalize_tags, unknown_tags


def test_normalize_coerces_shapes():
    assert normalize_tags(None) == []
    assert normalize_tags("Literature") == ["literature"]  # scalar -> list, lowercased
    assert normalize_tags(["literature"]) == ["literature"]


def test_normalize_dedups_sorts_and_canonicalizes_tokens():
    # "Case Checks" and "case_checks" both fold to one hyphen-joined token, duplicates
    # collapse, and the result is sorted for determinism.
    assert normalize_tags(["Case Checks", "case_checks", "literature", "literature"]) == \
        ["case-checks", "literature"]


def test_unknown_tags_flags_only_out_of_vocab():
    assert unknown_tags(["literature", "computation", "notation"]) == []
    assert unknown_tags(["literature", "sparkles"]) == ["sparkles"]
    # advisory helper still normalizes before checking, so casing never false-flags
    assert unknown_tags(["Literature"]) == []


def test_vocabulary_covers_the_named_starter_themes():
    # The themes the inbox docs promise authors (raw/_TEMPLATE.md) must actually exist.
    assert {"literature", "background", "mainline", "conjecture", "computation",
            "examples", "technique", "notation", "writeup"} <= set(MATERIAL_TAGS)

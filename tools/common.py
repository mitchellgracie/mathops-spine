"""Shared paths for the tooling. Repo root is the parent of this tools/ package."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANON_DIR = ROOT / "canon"
AGENTS_DIR = ROOT / "agents"
# Layer 2: exposition files — working notes, survey writeups, paper-draft sections —
# [[id]]-wiki-linked into canon (see tools.expositions). Written by `make extract-apply`
# or deliberate authoring, never a dumping ground for generated prose (CLAUDE.md rule 9).
EXPOSITIONS_DIR = ROOT / "expositions"
# Verification code the `computation` entities describe (schemas/entities.py): the code
# lives here, the knowledge-graph face lives in canon/computations/. Nothing in the
# gate executes this tree — a computation's `conclusion` is authored canon, and
# re-running the script is a human/agent act, not a CI step.
COMPUTATIONS_DIR = ROOT / "computations"
DERIVED_DIR = ROOT / "derived"
CAPSULES_DIR = DERIVED_DIR / "capsules"
INDICES_DIR = DERIVED_DIR / "indices"
# LLM-written, advisory, ungated critique notes — see tools.editor's module docstring
# for how this third artifact class differs from both the deterministic derived/ views
# (tools.build) and the hash-gated capsules (tools.capsules).
EDITOR_NOTES_DIR = DERIVED_DIR / "editor-notes"
# The extraction pipeline (tools.extract): raw material lands in raw/, moves to
# raw/extracted/ once applied; plans await triage in extraction/plans/ and are archived
# in extraction/approved/ after apply. None of these are canon or derived — raw is
# human-authored input, plans are LLM-proposed + human-edited workflow artifacts.
RAW_DIR = ROOT / "raw"
# The inbox is bucketed by what the material *is* (each bucket maps 1:1 to a `kind`; see
# tools.extract.FOLDER_KIND): papers/ = your own writeups of literature (statements,
# definitions, proofs — with citations; never the PDFs themselves), sessions/ =
# transcripts of research conversations/working sessions, drafts/ = your own prose bound
# for an exposition. extracted/ is the post-apply archive, not a kind. Files may still
# sit at raw/ root (kind declared inline). The bucket a file lives in only sets the
# *default* kind — frontmatter always wins — so these constants are for
# discovery/scaffolding, not enforcement.
RAW_PAPERS_DIR = RAW_DIR / "papers"
RAW_SESSIONS_DIR = RAW_DIR / "sessions"
RAW_DRAFTS_DIR = RAW_DIR / "drafts"
RAW_EXTRACTED_DIR = RAW_DIR / "extracted"
EXTRACTION_DIR = ROOT / "extraction"
EXTRACTION_PLANS_DIR = EXTRACTION_DIR / "plans"
EXTRACTION_APPROVED_DIR = EXTRACTION_DIR / "approved"
# Records the schema version the canon on disk is at (see tools.migrate). Kept out of
# canon/ so it never interacts with entity loading.
SCHEMA_VERSION_FILE = ROOT / ".schema-version"

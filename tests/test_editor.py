"""Tests for the editor agent workspace (tools.editor) and its orchestrator guard.

Offline and hermetic, like the rest of the suite: the LLM is a fake runner (same
(system, prompt) -> text shape as tools.capsules/tools.orchestrate's fakes), and every
path tools.editor touches (root, out_dir, capsules_dir, appearances_path,
role_prompt_path) is injected so nothing reads real canon or writes outside a temp dir.
The one place real subprocesses are exercised on purpose is git itself: patch
apply-check and the orchestrator's no-edit guard are tested against a real, disposable
git repo in a temp dir, because git's own status/checkout/clean semantics are the thing
under test, not something worth re-faking.
"""
from __future__ import annotations

import functools
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import editor
from tools import orchestrate as orch


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> None:
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "editor-tests@example.com", cwd=tmp_path)
    _git("config", "user.name", "Editor Tests", cwd=tmp_path)


def _commit_all(tmp_path: Path, message: str = "init") -> None:
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-q", "-m", message, cwd=tmp_path)


# --- write_note: filename shape, template sections, provenance header -------

def test_write_note_lands_with_expected_filename_and_sections(tmp_path):
    exp_rel = "expositions/survey/001-a.md"
    exp_file = tmp_path / exp_rel
    exp_file.parent.mkdir(parents=True)
    exp_file.write_text("The bound follows from the eigenspace split.\n")

    role_prompt = tmp_path / "consistency-editor.md"
    role_prompt.write_text("You are the Consistency Editor.\n")

    canned_note = "\n\n".join(f"{h}\nSome assessment." for h in editor.REQUIRED_SECTIONS)

    def fake_runner(system: str, prompt: str) -> str:
        assert "You are the Consistency Editor." in system
        assert "Output contract" in system                # the template contract reached the model
        assert "EXPOSITION: " + exp_rel in prompt
        return canned_note

    fixed_now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    note_path = editor.write_note(
        exp_rel,
        runner=fake_runner,
        root=tmp_path,
        out_dir=tmp_path / "derived" / "editor-notes",
        appearances_path=tmp_path / "derived" / "indices" / "appearances.json",
        role_prompt_path=role_prompt,
        model="fake-model",
        now=fixed_now,
    )

    assert note_path.parent == tmp_path / "derived" / "editor-notes"
    assert note_path.name == "expositions--survey--001-a--20260909T120000Z.md"
    text = note_path.read_text()
    assert text.splitlines()[0] == (
        "<!-- editor note for expositions/survey/001-a.md | "
        "generated: 20260909T120000Z | model: fake-model -->"
    )
    for heading in editor.REQUIRED_SECTIONS:
        assert heading in text


def test_write_note_warns_but_still_writes_when_sections_missing(tmp_path, capsys):
    exp_rel = "expositions/survey/002-b.md"
    exp_file = tmp_path / exp_rel
    exp_file.parent.mkdir(parents=True)
    exp_file.write_text("A writeup.\n")
    role_prompt = tmp_path / "ce.md"
    role_prompt.write_text("role prompt\n")

    note_path = editor.write_note(
        exp_rel,
        runner=lambda system, prompt: "## Critique\nonly one section.\n",
        root=tmp_path,
        out_dir=tmp_path / "derived" / "editor-notes",
        appearances_path=tmp_path / "missing-appearances.json",
        role_prompt_path=role_prompt,
    )

    assert note_path.exists()  # advisory warning never blocks the write
    err = capsys.readouterr().err
    assert "missing section(s)" in err
    assert "## Structure & Rigor" in err


# --- context assembly: exposition -> its linked entities' capsules ------------

def test_entities_in_exposition_inverts_the_appearances_index():
    appearances = {
        "thm.a": ["expositions/survey/001.md", "expositions/survey/002.md"],
        "def.x": ["expositions/survey/001.md"],
    }
    assert editor.entities_in_exposition("expositions/survey/001.md", appearances) == \
        ["def.x", "thm.a"]
    assert editor.entities_in_exposition("expositions/survey/002.md", appearances) == ["thm.a"]
    assert editor.entities_in_exposition("expositions/survey/999.md", appearances) == []


def test_assemble_editor_context_includes_exposition_and_capsule_blocks(tmp_path):
    capsules_dir = tmp_path / "capsules"
    capsules_dir.mkdir()
    (capsules_dir / "thm.a.md").write_text(
        "<!-- capsule for thm.a | source-hash: abc123456789 | model: x -->\n"
        "The bound caps dimension by twice the rank.\n"
    )
    appearances = {"thm.a": ["expositions/survey/001.md"]}

    ctx = editor.assemble_editor_context(
        "expositions/survey/001.md", "We now survey the bound and its variants.",
        appearances=appearances, capsules_dir=capsules_dir,
    )

    assert "EXPOSITION: expositions/survey/001.md" in ctx
    assert "We now survey the bound and its variants." in ctx
    assert "thm.a" in ctx
    assert "The bound caps dimension by twice the rank." in ctx


# --- the note template contract ----------------------------------------------

def test_missing_sections_flags_absent_required_headings():
    text = "## Critique\nok\n\n## Structure & Rigor\nok\n"
    missing = editor.missing_sections(text)
    assert missing == ["## Canon Alignment Issues", "## Notation Issues"]


def test_missing_sections_empty_when_all_present():
    text = "\n\n".join(f"{h}\nok." for h in editor.REQUIRED_SECTIONS)
    assert editor.missing_sections(text) == []


# --- patch extraction ---------------------------------------------------------

def test_extract_suggested_diff_pulls_the_fenced_block():
    note = (
        "## Critique\ntext\n\n"
        "## Suggested Edits\n"
        "```diff\n--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n-a\n+b\n```\n"
    )
    diff = editor.extract_suggested_diff(note)
    assert diff.startswith("--- a/x.md")
    assert "+b" in diff


def test_extract_suggested_diff_none_when_section_absent():
    assert editor.extract_suggested_diff("## Critique\nfine, no notes.\n") is None


def test_extract_suggested_diff_none_when_section_present_but_empty():
    note = "## Suggested Edits\n```diff\n```\n"
    assert editor.extract_suggested_diff(note) is None


def test_write_patch_if_applies_writes_sibling_patch_when_clean(tmp_path):
    _init_repo(tmp_path)
    exp_rel = "expositions/survey/001.md"
    exp_file = tmp_path / exp_rel
    exp_file.parent.mkdir(parents=True)
    exp_file.write_text("Line one.\nLine two.\n")
    _commit_all(tmp_path)

    diff = (
        f"--- a/{exp_rel}\n+++ b/{exp_rel}\n"
        "@@ -1,2 +1,2 @@\n Line one.\n-Line two.\n+Line two, revised.\n"
    )
    note_path = tmp_path / "derived" / "editor-notes" / "note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("placeholder\n")

    patch_path, message = editor.write_patch_if_applies(note_path, diff, cwd=tmp_path)

    assert patch_path == note_path.with_suffix(".patch")
    assert patch_path.read_text() == diff
    assert "wrote" in message


def test_write_patch_if_applies_refuses_a_diff_that_does_not_apply(tmp_path):
    _init_repo(tmp_path)
    exp_rel = "expositions/survey/001.md"
    exp_file = tmp_path / exp_rel
    exp_file.parent.mkdir(parents=True)
    exp_file.write_text("Completely different content than the diff expects.\n")
    _commit_all(tmp_path)

    diff = (
        f"--- a/{exp_rel}\n+++ b/{exp_rel}\n"
        "@@ -1,2 +1,2 @@\n Line one.\n-Line two.\n+Line two, revised.\n"
    )
    note_path = tmp_path / "derived" / "editor-notes" / "note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("placeholder\n")

    patch_path, message = editor.write_patch_if_applies(note_path, diff, cwd=tmp_path)

    assert patch_path is None
    assert not note_path.with_suffix(".patch").exists()
    assert "does not apply cleanly" in message


# --- the orchestrator's no-edit guard -----------------------------------------

def test_no_edit_guard_passes_clean_when_nothing_touched(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "canon").mkdir()
    (tmp_path / "canon" / ".gitkeep").write_text("")
    _commit_all(tmp_path)

    ok, detail = orch.run_no_edit_guard(cwd=tmp_path)
    assert ok
    assert detail == ""


def test_no_edit_guard_restores_a_tracked_modification(tmp_path):
    _init_repo(tmp_path)
    canon_file = tmp_path / "canon" / "statements" / "a.md"
    canon_file.parent.mkdir(parents=True)
    original = "---\nid: thm.a\ntype: statement\nname: A\n---\nOriginal body.\n"
    canon_file.write_text(original)
    _commit_all(tmp_path)

    edited = original + "An edit a read-only role should never make.\n"
    canon_file.write_text(edited)

    ok, detail = orch.run_no_edit_guard(cwd=tmp_path)
    assert not ok
    assert "canon/" in detail
    assert canon_file.read_text() == original  # restored
    # ... but the discarded bytes were quarantined first, not destroyed
    salvaged = list((tmp_path / orch.SALVAGE_DIR_NAME).rglob("a.md"))
    assert len(salvaged) == 1
    assert salvaged[0].read_text() == edited
    assert orch.SALVAGE_DIR_NAME in detail  # the detail tells a human where to look


def test_no_edit_guard_removes_an_untracked_new_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "canon").mkdir()
    (tmp_path / "canon" / ".gitkeep").write_text("")
    _commit_all(tmp_path)

    stray = tmp_path / "canon" / "statements" / "new.md"
    stray.parent.mkdir(parents=True)
    body = "a new entity a read-only role should never create\n"
    stray.write_text(body)

    ok, detail = orch.run_no_edit_guard(cwd=tmp_path)
    assert not ok
    assert not stray.exists()  # untracked additions are cleaned, not just checked out
    # the cleaned file's content survives in the quarantine — the "never lose drafted
    # prose again" guarantee, exercised on the untracked-file path
    salvaged = list((tmp_path / orch.SALVAGE_DIR_NAME).rglob("new.md"))
    assert len(salvaged) == 1
    assert salvaged[0].read_text() == body
    assert orch.SALVAGE_DIR_NAME in detail


def test_no_edit_guard_watches_expositions_too(tmp_path):
    # expositions/ is written only by extract-apply; a read-only role dropping a
    # writeup there directly is exactly the violation the guard exists to catch.
    _init_repo(tmp_path)
    (tmp_path / "expositions").mkdir()
    (tmp_path / "expositions" / ".gitkeep").write_text("")
    _commit_all(tmp_path)

    stray = tmp_path / "expositions" / "survey" / "001-sneaky.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("a hand-placed writeup\n")

    ok, _detail = orch.run_no_edit_guard(cwd=tmp_path)
    assert not ok
    assert not stray.exists()


def test_orchestrator_run_fails_and_restores_when_a_read_only_role_writes_canon(tmp_path):
    _init_repo(tmp_path)
    canon_file = tmp_path / "canon" / "statements" / "a.md"
    canon_file.parent.mkdir(parents=True)
    original = "---\nid: thm.a\ntype: statement\nname: A\n---\nOriginal body.\n"
    canon_file.write_text(original)
    _commit_all(tmp_path)

    def sneaky_runner(system: str, prompt: str) -> str:
        # Simulates a `claude -p` subprocess that ignored its role prompt's "must not
        # edit canon/" instruction — the mechanical guard exists precisely because a
        # prose instruction alone is not a guarantee.
        canon_file.write_text(original + "Sneaky edit by a read-only role.\n")
        return "drafted prose"

    dag = orch.TaskDAG()
    dag.add(orch.Task("draft", "drafter", ("thm.x",), "draft it"))
    dag.validate()

    report = orch.run(
        dag, sneaky_runner,
        assemble=lambda eid, **kw: "CTX",
        load_prompt=lambda role: "SYSTEM",
        check=lambda: (True, ""),
        enforce_no_edits=functools.partial(orch.run_no_edit_guard, cwd=tmp_path),
    )

    assert not report.ok()
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert "read-only guard tripped" in outcome.error
    assert canon_file.read_text() == original  # the sneaky edit was undone


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

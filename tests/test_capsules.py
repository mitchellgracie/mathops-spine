"""Tests for the LLM capsule generator (tools.capsules).

The Anthropic API call is mocked with a recording fake client, so these run
offline with no key and no cost. They pin the contract that matters: a small/fast
model is used, the entity's frontmatter + body reach the prompt, and the output
lands at derived/capsules/<id>.md with a provenance marker.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import capsules
from tools.loader import load_canon


class RecordingClient:
    """Stand-in for anthropic.Anthropic: records create() kwargs, returns canned text."""

    def __init__(self, reply: str = "The widget bound caps dimension by twice the rank."):
        self.reply = reply
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=outer.reply)]
                )

        self.messages = _Messages()


def write(tmp: Path, relpath: str, body: str) -> None:
    p = tmp / "canon" / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())


THM = """
    ---
    id: thm.bound
    type: statement
    name: Widget bound
    kind: theorem
    statement: |
      For every widget over $k$, $\\dim V \\le 2\\,\\mathrm{rk}(\\phi+\\mathrm{id})$.
    status: proved
    stated_in: src.paper
    ---
    The running example theorem; the bound is sharp for $k$ algebraically closed.
"""
SOURCE = """
    ---
    id: src.paper
    type: source
    name: Example Paper
    year: 2024
    ---
    body
"""


def _canon(tmp_path: Path):
    write(tmp_path, "statements/bound.md", THM)
    write(tmp_path, "sources/paper.md", SOURCE)
    return load_canon(tmp_path / "canon")


def test_generate_capsule_api_uses_small_model_and_source(tmp_path):
    canon = _canon(tmp_path)
    client = RecordingClient("cap text")
    text = capsules.generate_capsule_api(client, canon.entities["thm.bound"],
                                         canon.bodies["thm.bound"])

    assert text == "cap text"
    assert len(client.calls) == 1
    call = client.calls[0]
    # small/fast tier, bounded output, a system prompt present
    assert call["model"] == capsules.CAPSULE_MODEL == "claude-haiku-4-5"
    assert isinstance(call["max_tokens"], int) and call["max_tokens"] > 0
    assert call["system"].strip()
    # the entity's own frontmatter (LaTeX statement included) + prose body both reach
    # the prompt, faithfully
    user = call["messages"][0]["content"]
    assert "thm.bound" in user and "Widget bound" in user            # frontmatter
    assert "\\dim V \\le 2\\,\\mathrm{rk}" in user                   # LaTeX untouched
    assert "sharp for $k$ algebraically closed" in user              # body


class FakeRun:
    """Stand-in for subprocess.run: records the argv, returns a canned result."""

    def __init__(self, stdout: str = "A capsule.", returncode: int = 0, stderr: str = ""):
        self.result = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
        self.cmd: list[str] | None = None
        self.kwargs: dict | None = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        return self.result


def test_generate_capsule_cli_builds_command_and_returns_text(tmp_path):
    canon = _canon(tmp_path)
    run = FakeRun("The bound caps dimension.\n")
    text = capsules.generate_capsule_cli(
        canon.entities["thm.bound"], canon.bodies["thm.bound"], binary="claude", run=run,
    )

    assert text == "The bound caps dimension."          # stdout, stripped
    cmd = run.cmd
    assert cmd[0] == "claude" and "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == capsules.CAPSULE_MODEL == "claude-haiku-4-5"
    assert cmd[cmd.index("--system-prompt") + 1] == capsules.SYSTEM_PROMPT
    assert "Widget bound" in cmd[-1]                     # rendered source is the prompt
    assert run.kwargs["capture_output"] and run.kwargs["text"]
    assert run.kwargs["timeout"] == capsules.CLI_TIMEOUT


def test_generate_capsule_cli_raises_on_nonzero_exit(tmp_path):
    canon = _canon(tmp_path)
    run = FakeRun(stdout="", returncode=1, stderr="model unavailable")
    try:
        capsules.generate_capsule_cli(canon.entities["thm.bound"], "", binary="claude", run=run)
    except RuntimeError as exc:
        assert "model unavailable" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on non-zero exit")


def test_build_capsules_writes_contract_file(tmp_path):
    canon = _canon(tmp_path)
    client = RecordingClient("The bound, proved in Example Paper.")
    out = tmp_path / "caps"

    rc = capsules.build_capsules(client=client, canon=canon, out_dir=out)

    assert rc == 0
    # one API call per entity
    assert len(client.calls) == len(canon.entities) == 2
    f = out / "thm.bound.md"
    assert f.exists()
    content = f.read_text()
    assert "The bound, proved in Example Paper." in content
    # first-line marker: id + source-hash + model, and it is fresh vs the source
    assert content.startswith("<!-- capsule for thm.bound | source-hash: ")
    assert capsules.CAPSULE_MODEL in content
    h, is_ph = capsules.parse_marker(content)
    assert h == capsules.hash_for(canon, "thm.bound") and not is_ph
    assert content.endswith("\n")


def test_build_capsules_targets_subset(tmp_path):
    canon = _canon(tmp_path)
    client = RecordingClient("just one")
    out = tmp_path / "caps"

    rc = capsules.build_capsules(["thm.bound"], client=client, canon=canon, out_dir=out)

    assert rc == 0
    assert len(client.calls) == 1
    assert (out / "thm.bound.md").exists()
    assert not (out / "src.paper.md").exists()


def test_build_capsules_rejects_unknown_id(tmp_path):
    canon = _canon(tmp_path)
    client = RecordingClient()
    rc = capsules.build_capsules(["thm.nope"], client=client, canon=canon,
                                 out_dir=tmp_path / "caps")

    assert rc == 1
    assert client.calls == []  # never hit the API


def test_build_capsules_refuses_on_canon_errors(tmp_path):
    # unknown frontmatter field -> a load/validation error -> must not call the API
    write(tmp_path, "statements/bad.md", """
        ---
        id: thm.b
        type: statement
        name: B
        nonsense_field: 7
        ---
        body
    """)
    canon = load_canon(tmp_path / "canon")
    assert canon.errors
    client = RecordingClient()

    rc = capsules.build_capsules(client=client, canon=canon, out_dir=tmp_path / "caps")

    assert rc == 1
    assert client.calls == []


def test_empty_reply_leaves_file_untouched(tmp_path):
    canon = _canon(tmp_path)
    client = RecordingClient("   ")  # whitespace-only -> empty after strip
    out = tmp_path / "caps"

    rc = capsules.build_capsules(["thm.bound"], client=client, canon=canon, out_dir=out)

    assert rc == 0
    assert not (out / "thm.bound.md").exists()  # skipped rather than writing a blank capsule


def _generate(tmp_path, reply="A capsule for the bound."):
    """Write a tiny canon and generate real (non-placeholder) capsules into out/."""
    canon = _canon(tmp_path)
    out = tmp_path / "caps"
    capsules.build_capsules(client=RecordingClient(reply), canon=canon, out_dir=out)
    return canon, out


def test_build_capsules_skips_fresh(tmp_path):
    canon = _canon(tmp_path)
    out = tmp_path / "caps"
    c1 = RecordingClient("v1")
    capsules.build_capsules(client=c1, canon=canon, out_dir=out)
    assert len(c1.calls) == 2  # first run generates both

    c2 = RecordingClient("v2")
    capsules.build_capsules(client=c2, canon=canon, out_dir=out)
    assert c2.calls == []  # second run: all fresh, nothing regenerated

    c3 = RecordingClient("v3")
    capsules.build_capsules(client=c3, canon=canon, out_dir=out, force=True)
    assert len(c3.calls) == 2  # --force regenerates regardless


def test_check_passes_on_fresh_capsules(tmp_path):
    canon, out = _generate(tmp_path)
    rep = capsules.check_capsules(canon, capsules_dir=out)
    assert rep.ok(), rep.errors


def test_check_flags_stale_after_source_edit(tmp_path):
    canon, out = _generate(tmp_path)
    # A change to meaning (prose body here) must invalidate the capsule. The freshness
    # gate compares the on-disk capsule against the loaded canon, so edit the source and
    # reload — the reloaded semantic content no longer matches the capsule's stamp.
    canon.paths["thm.bound"].write_text(canon.paths["thm.bound"].read_text() + "\nEdited.\n")
    reloaded = load_canon(tmp_path / "canon")
    rep = capsules.check_capsules(reloaded, capsules_dir=out)
    assert any("stale capsule: thm.bound" in e for e in rep.errors), rep.errors
    assert not any("stale capsule: src.paper" in e for e in rep.errors), rep.errors


def test_check_ignores_pure_reformat(tmp_path):
    # The defining property of semantic-content hashing: reordering frontmatter keys and
    # restyling YAML (what tools.fmt does) preserves meaning, so a capsule stays fresh.
    canon, out = _generate(tmp_path)
    reformatted = """
        ---
        type: statement
        id: thm.bound
        name: Widget bound
        stated_in: src.paper
        status: proved
        kind: theorem
        statement: |
          For every widget over $k$, $\\dim V \\le 2\\,\\mathrm{rk}(\\phi+\\mathrm{id})$.
        ---
        The running example theorem; the bound is sharp for $k$ algebraically closed.
    """
    write(tmp_path, "statements/bound.md", reformatted)
    reloaded = load_canon(tmp_path / "canon")
    rep = capsules.check_capsules(reloaded, capsules_dir=out)
    assert not any("stale capsule: thm.bound" in e for e in rep.errors), rep.errors


def test_check_flags_missing_capsule(tmp_path):
    canon = _canon(tmp_path)
    rep = capsules.check_capsules(canon, capsules_dir=tmp_path / "empty")
    assert any("missing capsule: thm.bound" in e for e in rep.errors), rep.errors


def test_check_flags_orphan_capsule(tmp_path):
    canon, out = _generate(tmp_path)
    (out / "thm.ghost.md").write_text(
        "<!-- capsule for thm.ghost | source-hash: 000000000000 -->\nx\n")
    rep = capsules.check_capsules(canon, capsules_dir=out)
    assert any("orphan capsule: thm.ghost.md" in e for e in rep.errors), rep.errors


def test_check_flags_capsule_without_hash_marker(tmp_path):
    canon, out = _generate(tmp_path)
    (out / "thm.bound.md").write_text("<!-- AUTOGENERATED capsule for thm.bound -->\nold\n")
    rep = capsules.check_capsules(canon, capsules_dir=out)
    assert any("no source-hash marker: thm.bound" in e for e in rep.errors), rep.errors


def test_bootstrap_creates_placeholders_without_touching_existing(tmp_path):
    canon = _canon(tmp_path)
    out = tmp_path / "caps"
    # a real capsule already exists for thm.bound
    capsules.build_capsules(["thm.bound"], client=RecordingClient("real capsule"),
                            canon=canon, out_dir=out)
    before = (out / "thm.bound.md").read_text()

    capsules.bootstrap_capsules(canon, out_dir=out)

    assert (out / "thm.bound.md").read_text() == before       # untouched
    src = (out / "src.paper.md").read_text()                  # created
    _, is_ph = capsules.parse_marker(src)
    assert is_ph and capsules.PLACEHOLDER_TAG in src.splitlines()[0]
    # placeholder is fresh (has the right hash) so it passes the non-strict gate...
    rep = capsules.check_capsules(canon, capsules_dir=out)
    assert rep.ok(), rep.errors
    assert any("placeholder capsule: src.paper" in w for w in rep.warnings)
    # ...but --strict fails on it
    strict = capsules.check_capsules(canon, capsules_dir=out, strict=True)
    assert any("placeholder capsule: src.paper" in e for e in strict.errors)


def test_tombstones_carry_no_capsule(tmp_path):
    write(tmp_path, "statements/bound.md", THM)
    write(tmp_path, "sources/paper.md", SOURCE)
    write(tmp_path, "statements/dup.md", """
        ---
        id: thm.dup
        type: statement
        name: Duplicate bound
        merged_into: thm.bound
        ---
        Merged into [[thm.bound]].
    """)
    canon = load_canon(tmp_path / "canon")
    out = tmp_path / "caps"
    client = RecordingClient("cap")
    capsules.build_capsules(client=client, canon=canon, out_dir=out)
    assert not (out / "thm.dup.md").exists()
    rep = capsules.check_capsules(canon, capsules_dir=out)
    assert rep.ok(), rep.errors  # no missing-capsule error for the tombstone


def test_build_no_longer_emits_capsules(tmp_path):
    # the deterministic build must not write capsules
    from tools import build

    assert not hasattr(build, "emit_capsules")
    assert not hasattr(build, "_capsule_text")


def test_capsule_body_prefers_real_file_then_placeholder(tmp_path):
    canon, out = _generate(tmp_path, reply="Real bound capsule.")
    # real file present -> its prose, marker stripped
    assert capsules.capsule_body("thm.bound", canon.entities["thm.bound"], capsules_dir=out) \
        == "Real bound capsule."
    # no file -> deterministic placeholder from frontmatter
    body = capsules.capsule_body("src.paper", canon.entities["src.paper"],
                                 capsules_dir=tmp_path / "none")
    assert "Example Paper" in body


def test_render_source_excludes_unintegrated_blocks(tmp_path):
    # An unintegrated extraction block (tools.provenance) is workflow state: invisible
    # to the generator's input and the freshness hash until integration rewrites prose.
    from tools.provenance import Note, append_block, render_block

    canon = _canon(tmp_path)
    before = capsules.hash_for(canon, "thm.bound")
    block = render_block([Note("The bound extends to characteristic 2.")],
                         date="2026-09-09", source="raw/extracted/x.md",
                         plan="extraction/approved/x.md")
    path = tmp_path / "canon" / "statements" / "bound.md"
    path.write_text(append_block(path.read_text(), block))
    canon = load_canon(tmp_path / "canon")
    src = capsules.render_source(canon.entities["thm.bound"], canon.bodies["thm.bound"])
    assert "characteristic 2" not in src
    assert capsules.hash_for(canon, "thm.bound") == before


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

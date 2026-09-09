"""Capsules: the terse per-entity summaries under derived/capsules/<id>.md.

Capsules are the one derived artifact that is NOT a deterministic function of canon
(an LLM writes them), so they can't ride the "regenerate and byte-diff" drift guard
the other derived artifacts use. Instead each capsule embeds a hash of its source and
freshness is checked by comparing that hash to the current source — staleness, not
byte-identity. The hash is taken over the entity's *semantic content* — the exact
frontmatter+body projection the capsule is generated from (`render_source`), not the
raw file bytes. That makes staleness mean "the thing the capsule summarizes changed":
a pure reformat (tools.fmt reorders keys / restyles YAML) is correctly neutral, while
any change to a field value or the prose invalidates the capsule. This module owns the
whole capsule lifecycle:

  generate (LLM)   `python -m tools.capsules`            # stale/missing only; --force all
  bootstrap        `python -m tools.capsules --bootstrap`# create missing PLACEHOLDERS, no LLM
  check            `python -m tools.capsules --check`    # freshness gate, no LLM/network
  restamp          `python -m tools.capsules --restamp`  # rewrite the hash marker in place

Generation has two backends (same prompt):
  * ``cli`` (default) — shells out to `claude -p`, which runs on your Claude
    subscription (Pro/Max), not metered API tokens. Needs the `claude` CLI reachable
    (via $CLAUDE_CODE_EXECPATH when launched inside Claude Code, or on $PATH).
  * ``api`` — Anthropic Messages API via the `anthropic` SDK. Metered; needs
    `ANTHROPIC_API_KEY`. The fallback for anyone with API billing.

Capsules are single-block prose stating everything the source states, plainly: canon
is the author's omniscient ledger and carries no secrecy marking, so neither does any
projection of it (the ancestor spine's two-block/spoiler-gated capsule format and its
leak audit were dismantled with the rest of its knowledge-gating machinery; what a
consumer may know is that consumer's construction-time config, not a property of the
stored artifact).

`make build` does NOT touch capsules (see tools.build). Run `make validate` first;
generation and check both refuse to run on a canon that fails to load.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .common import CAPSULES_DIR
from .loader import Canon, load_canon
from .provenance import strip_blocks
from .validate import Report

# Small/fast tier: capsule writing is summarization — the cheap, high-volume end of
# the tiered-model routing — so it defaults to a Haiku-class model. Override with --model.
CAPSULE_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 512          # capsules are 2-5 sentences; bounds cost/length
CLI_TIMEOUT = 240         # per-entity ceiling for the `claude -p` subprocess

# First-line provenance/freshness marker. Parsed by the checker; never mistaken for
# canon. `PLACEHOLDER` present => a bootstrap stub, not real LLM content.
_HASH_RE = re.compile(r"source-hash:\s*([0-9a-f]{12})")
PLACEHOLDER_TAG = "PLACEHOLDER"

SYSTEM_PROMPT = """\
You write capsules for a mathematics research knowledge base. A capsule is a terse, \
factual summary of a single entity — 2-5 sentences — used as bounded background \
context when an agent works on other entities. It is a derived projection of the \
canon, never canon itself.

Rules:
- Use ONLY what the entity's frontmatter and body state. Never add hypotheses, \
strengthen or weaken a statement, or speculate about what something "likely" says \
or implies. A thin source yields a short capsule; do not pad it.
- For a statement or definition, the mathematical content comes first and must be \
FAITHFUL: reproduce the hypotheses and conclusion precisely, keeping the source's \
LaTeX notation exactly as written — never re-notate, simplify, or paraphrase a \
formula. Then its status/provenance (proved where, conjectured, refuted by what).
- For a proof, summarize the strategy and what it uses — not the argument line by \
line. For a computation, say what it checks, over what range, and what it concluded.
- Match the source's register: neutral, present-tense, precise. No dramatization, \
no hedging the source doesn't have.
- Start with the entity's name or content, not a filler opener like "This theorem \
is". Refer to related entities as they appear in the source.
- Plain prose (inline LaTeX allowed): no headings, no lists, no markdown \
formatting, no surrounding quotes, and no preamble or sign-off.
- Output only the capsule text."""


# --- hashing + marker (single source of truth for generate AND check) --------

def source_hash(data: bytes) -> str:
    """Stable 12-hex-char digest of a byte string."""
    return hashlib.sha256(data).hexdigest()[:12]


def hash_for(canon: Canon, entity_id: str) -> str:
    """Freshness digest of an entity's *semantic content*, not its file bytes.

    Hashing `render_source` (the frontmatter+body projection the capsule is generated
    from) rather than the raw file means a pure reformat — key reordering, quoting or
    list-style changes from tools.fmt — does not invalidate a capsule, while any change
    to a field value or the prose does. This is the definition of staleness we actually
    want: "did the thing the capsule summarizes change?"
    """
    entity = canon.entities[entity_id]
    body = canon.bodies.get(entity_id, "")
    return source_hash(render_source(entity, body).encode("utf-8"))


def parse_marker(content: str) -> tuple[str | None, bool]:
    """Return (source_hash | None, is_placeholder) parsed from a capsule's first line."""
    first = content.splitlines()[0] if content else ""
    m = _HASH_RE.search(first)
    return (m.group(1) if m else None), (PLACEHOLDER_TAG in first)


def capsule_markdown(entity_id: str, text: str, *, src_hash: str,
                     model: str | None = None, placeholder: bool = False) -> str:
    """Wrap capsule prose with the freshness/provenance marker on line 1."""
    fields = [f"capsule for {entity_id}", f"source-hash: {src_hash}"]
    fields.append(PLACEHOLDER_TAG if placeholder else f"model: {model}")
    return f"<!-- {' | '.join(fields)} -->\n{text}\n"


def capsule_body(entity_id: str, entity=None, *, capsules_dir: Path = CAPSULES_DIR) -> str:
    """The capsule prose for an entity (marker stripped), for the assembler's cheap tier.

    Returns the real derived capsule if one exists, else the deterministic placeholder
    text (when `entity` is given). This is the single reader of the on-disk capsule
    format, so callers don't need to know the marker line.
    """
    cap_path = Path(capsules_dir) / f"{entity_id}.md"
    if cap_path.exists():
        lines = cap_path.read_text().splitlines()
        if lines and lines[0].lstrip().startswith("<!--"):
            lines = lines[1:]
        return "\n".join(lines).strip()
    return _placeholder_capsule_text(entity).strip() if entity is not None else ""


def render_source(entity, body: str) -> str:
    """Render an entity's frontmatter + prose body as the model's input.

    Unintegrated extraction blocks (tools.provenance) ARE excluded from the body: they
    are workflow state, not settled canon, and hashing them would stale a capsule the
    moment an extraction lands, forcing a regeneration from exactly the uncompacted
    prose it shouldn't summarize. Excluded, the capsule stays fresh across an append
    and goes stale at integration, when the body's real prose actually changes.

    Hash-basis note (CLAUDE.md rule 7): this projection is what the freshness hash
    covers, so any change here moves hashes. Dropping the spoiler machinery changed it
    only for entities that carried `spoiler: true` relations (the flag no longer
    renders because the field no longer exists) — exactly the capsules that needed
    regeneration to lose their gated blocks; every other entity's projection is
    byte-identical to the v1-era output.
    """
    data = _strip_ordinals(entity.model_dump(mode="json", exclude_none=True))
    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    body = strip_blocks(body or "").strip()
    parts = [f"Frontmatter:\n{fm}"]
    if body:
        parts.append(f"Body:\n{body}")
    return "\n\n".join(parts)


def _strip_ordinals(obj):
    """Drop computed `ordinal` keys so rendered dates read as authored (y/m/d)."""
    if isinstance(obj, dict):
        return {k: _strip_ordinals(v) for k, v in obj.items() if k != "ordinal"}
    if isinstance(obj, list):
        return [_strip_ordinals(v) for v in obj]
    return obj


def _placeholder_capsule_text(entity) -> str:
    """Deterministic placeholder prose synthesized from frontmatter (bootstrap only).

    Used only to give a brand-new entity *some* capsule so the freshness gate has a
    file to check; it carries a visible PLACEHOLDER marker and is replaced by real
    LLM content on the next `make build-capsules`.
    """
    facts = []
    for key in ("kind", "status", "statement", "proves", "completeness", "strategy",
                "year", "venue", "bibkey", "path", "language", "covers", "conclusion"):
        val = getattr(entity, key, None)
        if val:
            facts.append(f"{key}: {val}")
    body = entity.summary or f"{entity.name} ({entity.type})."
    if facts:
        body += "\n" + "; ".join(facts) + "."
    return body


# --- backends ----------------------------------------------------------------
#
# One LLM invocation path, two interchangeable backends: `run_llm_cli` (subscription,
# via `claude -p`) and `run_llm_api` (metered Messages API). Both take a bare
# (system_prompt, user_prompt) and return completion text, so they are not
# capsule-specific — the role dispatcher in tools.orchestrate reuses them, and there is
# exactly one place in the repo that shells out to the CLI and one that hits the API.
# The capsule generators below are thin specializations that fix the system prompt and
# build the user prompt from an entity.

def _claude_binary(explicit: str | None = None) -> str:
    binary = explicit or os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if not binary:
        raise RuntimeError(
            "claude CLI not found. Set CLAUDE_CODE_EXECPATH, put `claude` on PATH, "
            "or use --backend api. Install: npm i -g @anthropic-ai/claude-code."
        )
    return binary


def run_llm_cli(system_prompt: str, prompt: str, *, model: str,
                binary: str | None = None, run=subprocess.run,
                cwd: str | None = None, timeout: int = CLI_TIMEOUT) -> str:
    """Run one (system, user) prompt through `claude -p` and return its text.

    `run` is injected for testing. Runs from a neutral cwd so the repo's CLAUDE.md
    isn't loaded as ambient context, and passes an explicit --system-prompt so the
    default coding-agent framing is replaced by the caller's role/style prompt.
    """
    cmd = [
        _claude_binary(binary),
        "-p",
        "--model", model,
        "--output-format", "text",
        "--system-prompt", system_prompt,
        prompt,
    ]
    result = run(cmd, capture_output=True, text=True,
                 cwd=cwd or tempfile.gettempdir(), timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip()


def run_llm_api(client, system_prompt: str, prompt: str, *, model: str,
                max_tokens: int = MAX_TOKENS) -> str:
    """Run one (system, user) prompt through the Anthropic Messages API.

    `client` is an `anthropic.Anthropic` (injected so this is trivially testable).
    """
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def generate_capsule_api(client, entity, body: str, *, model: str = CAPSULE_MODEL) -> str:
    """Write one capsule via the Messages API (the metered backend)."""
    return run_llm_api(client, SYSTEM_PROMPT, render_source(entity, body), model=model)


def generate_capsule_cli(entity, body: str, *, model: str = CAPSULE_MODEL,
                         binary: str | None = None, run=subprocess.run,
                         cwd: str | None = None) -> str:
    """Write one capsule via `claude -p` (runs on the Claude subscription)."""
    return run_llm_cli(SYSTEM_PROMPT, render_source(entity, body),
                       model=model, binary=binary, run=run, cwd=cwd)


# --- generate / bootstrap ----------------------------------------------------

def build_capsules(
    ids: list[str] | None = None,
    *,
    backend: str | None = None,
    client=None,
    model: str = CAPSULE_MODEL,
    force: bool = False,
    canon: Canon | None = None,
    out_dir: Path = CAPSULES_DIR,
) -> int:
    """(Re)generate capsules via an LLM. Skips capsules already fresh unless `force`.

    `backend` is "cli" (default) or "api"; passing a `client` implies "api". Fresh =
    the on-disk capsule's embedded source-hash matches the current source AND it is
    not a placeholder. Injectable params (`client`, `canon`, `out_dir`) are for tests.
    """
    if backend is None:
        backend = "api" if client is not None else "cli"
    if backend not in ("cli", "api"):
        print(f"Unknown backend: {backend!r} (expected 'cli' or 'api')")
        return 1

    if canon is None:
        canon = load_canon()
    if canon.errors:
        _print_load_errors(canon)
        return 1

    targets = ids or list(canon.entities)
    missing = [eid for eid in targets if eid not in canon.entities]
    if missing:
        print(f"Unknown entity id(s): {', '.join(missing)}")
        return 1

    if backend == "api" and client is None:
        import anthropic  # imported lazily: not needed for the cli backend or tests

        client = anthropic.Anthropic()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for eid in targets:
        if canon.entities[eid].merged_into is not None:
            continue  # tombstones carry no capsule
        cur_hash = hash_for(canon, eid)
        cap_path = out_dir / f"{eid}.md"
        if not force and cap_path.exists():
            h, is_ph = parse_marker(cap_path.read_text())
            if h == cur_hash and not is_ph:
                skipped += 1
                continue

        entity, body = canon.entities[eid], canon.bodies.get(eid, "")
        if backend == "cli":
            text = generate_capsule_cli(entity, body, model=model)
        else:
            text = generate_capsule_api(client, entity, body, model=model)
        if not text:
            print(f"  WARNING: empty capsule for {eid}; leaving existing file untouched")
            continue
        cap_path.write_text(capsule_markdown(eid, text, src_hash=cur_hash, model=model))
        print(f"  wrote derived/capsules/{eid}.md")
        written += 1

    print(f"Generated {written} capsule(s) with {model} ({backend}); {skipped} already fresh.")
    return 0


def bootstrap_capsules(canon: Canon | None = None, *, out_dir: Path = CAPSULES_DIR) -> int:
    """Create a PLACEHOLDER capsule for every entity that has none. Never overwrites.

    A no-LLM way to satisfy the freshness gate for brand-new entities; the placeholder
    is replaced by real content on the next `make build-capsules`.
    """
    if canon is None:
        canon = load_canon()
    if canon.errors:
        _print_load_errors(canon)
        return 1

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for eid, entity in canon.entities.items():
        if entity.merged_into is not None:
            continue  # tombstones carry no capsule
        cap_path = out_dir / f"{eid}.md"
        if cap_path.exists():
            continue
        text = _placeholder_capsule_text(entity)
        cap_path.write_text(
            capsule_markdown(eid, text, src_hash=hash_for(canon, eid), placeholder=True)
        )
        print(f"  bootstrapped placeholder derived/capsules/{eid}.md")
        created += 1
    print(f"Bootstrapped {created} placeholder capsule(s).")
    return 0


def restamp_capsules(canon: Canon | None = None, *, out_dir: Path = CAPSULES_DIR) -> int:
    """Rewrite each existing capsule's source-hash marker in place, keeping its prose.

    A maintenance escape hatch for exactly two situations: the hash *basis* changed (a
    tooling change, like moving from raw-byte to semantic-content hashing), or a pure
    reformat rewrote the source. In both, the capsule prose is still accurate but its
    embedded hash no longer matches, so the freshness gate would false-positive.

    HAZARD: this trusts that the existing prose still reflects the source. Never use it
    to silence a genuine content edit — that is what `make build-capsules` is for. It
    only touches the marker line; placeholder status and model provenance are preserved.
    """
    if canon is None:
        canon = load_canon()
    if canon.errors:
        _print_load_errors(canon)
        return 1

    out_dir = Path(out_dir)
    restamped = 0
    for eid in canon.entities:
        cap_path = out_dir / f"{eid}.md"
        if not cap_path.exists():
            continue
        lines = cap_path.read_text().splitlines(keepends=True)
        if not lines or not _HASH_RE.search(lines[0]):
            continue  # no parseable marker; leave it for --check to flag
        new_line, n = _HASH_RE.subn(f"source-hash: {hash_for(canon, eid)}", lines[0], count=1)
        if n and new_line != lines[0]:
            lines[0] = new_line
            cap_path.write_text("".join(lines))
            print(f"  restamped derived/capsules/{eid}.md")
            restamped += 1
    print(f"Restamped {restamped} capsule(s).")
    return 0


# --- freshness check (no LLM/network) ----------------------------------------

def check_capsules(canon: Canon, *, capsules_dir: Path = CAPSULES_DIR,
                   strict: bool = False) -> Report:
    """Verify every entity has a fresh capsule; report all problems at once.

    ERROR on: missing capsule, missing/garbled hash marker, stale hash, orphan file.
    Placeholders are WARN (they pass staleness); `strict` promotes them to ERROR.
    """
    rep = Report()
    capsules_dir = Path(capsules_dir)
    # Tombstones (merged-away entities) are retired: they carry no capsule, so exclude
    # them from both the freshness sweep and the orphan check below.
    entity_ids = {eid for eid, e in canon.entities.items() if e.merged_into is None}

    for eid in sorted(entity_ids):
        cap_path = capsules_dir / f"{eid}.md"
        if not cap_path.exists():
            rep.error(f"missing capsule: {eid} (run `make build-capsules` or `--bootstrap`)")
            continue
        content = cap_path.read_text()
        h, is_ph = parse_marker(content)
        if h is None:
            rep.error(f"no source-hash marker: {eid} (regenerate the capsule)")
            continue
        if h != hash_for(canon, eid):
            rep.error(f"stale capsule: {eid} (source changed since it was generated)")
            continue
        if is_ph:
            if strict:
                rep.error(f"placeholder capsule: {eid} (generate real content)")
            else:
                rep.warn(f"placeholder capsule: {eid}")

    if capsules_dir.exists():
        for cap_path in sorted(capsules_dir.glob("*.md")):
            if cap_path.stem not in entity_ids:
                rep.error(f"orphan capsule: {cap_path.name} (no entity {cap_path.stem})")
    return rep


# --- CLI ---------------------------------------------------------------------

def _print_load_errors(canon: Canon) -> None:
    print("Refusing to run: canon has load/validation errors. Run validate first.")
    for path, msg in canon.errors:
        print(f"  {path}: {msg}")


def _check_main(strict: bool) -> int:
    canon = load_canon()
    if canon.errors:
        _print_load_errors(canon)
        return 1
    rep = check_capsules(canon, strict=strict)
    for w in rep.warnings:
        print(f"WARN  {w}")
    for e in rep.errors:
        print(f"ERROR {e}")
    if rep.ok():
        print(f"\nOK — {len(canon.entities)} capsules fresh, {len(rep.warnings)} warning(s).")
        return 0
    print(f"\nFAILED — {len(rep.errors)} error(s), {len(rep.warnings)} warning(s).")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate / bootstrap / check capsules (derived/capsules/<id>.md).",
    )
    parser.add_argument("ids", nargs="*", help="entity ids to target; default: all")
    parser.add_argument("--only", action="append", default=[], metavar="ID",
                        help="target a specific entity (repeatable); same as a positional id")
    parser.add_argument("--check", action="store_true", help="freshness gate, no LLM/network")
    parser.add_argument("--strict", action="store_true", help="with --check: fail on placeholders")
    parser.add_argument("--bootstrap", action="store_true",
                        help="create missing PLACEHOLDER capsules, no LLM")
    parser.add_argument("--restamp", action="store_true",
                        help="rewrite hash markers in place (after a reformat / hash-basis change)")
    parser.add_argument("--force", action="store_true", help="regenerate even fresh capsules")
    parser.add_argument("--backend", choices=("cli", "api"), default="cli",
                        help="cli: `claude -p` on your subscription (default); api: metered API")
    parser.add_argument("--model", default=CAPSULE_MODEL, help=f"model (default: {CAPSULE_MODEL})")
    args = parser.parse_args(argv)

    if args.check:
        return _check_main(args.strict)
    if args.restamp:
        return restamp_capsules()
    if args.bootstrap:
        return bootstrap_capsules()
    targets = (args.ids + args.only) or None
    return build_capsules(targets, backend=args.backend, model=args.model, force=args.force)


if __name__ == "__main__":
    sys.exit(main())

"""LLM canon-check: does a diff's modified entities now contradict their
surrounding canon? Run as: python -m tools.canon_check [--base <ref>] [--model M]
[--backend cli|api] [--json] [--strict]

This is the **LLM** half of the continuity-check pipeline;
`tools.impact` is the deterministic half (*what* structurally changed and *what* it
touches). This module consumes that output as bounded context for one cheap,
bounded, advisory LLM call per modified entity — it never tries to be a second
validator; the deterministic gate (`tools.validate`) still owns every hard invariant.

**Scope: modified entities only.** Of `tools.pr_annotate`'s three diff buckets (new /
changed / removed), this module only checks `changed_ids`. A brand-new entity has
nothing yet to contradict (the same reasoning `tools.impact`'s module docstring gives
for excluding it from impact analysis); a removed entity has no current source left to
check for a contradiction, and the impact report already tells a reviewer everything
that pointed at it. Restricting to `changed_ids` also means every one of the four
context pieces below (current source, diff, neighbors, impact) is always meaningful —
no branch has to improvise a "what if the file doesn't exist" case.

**Context budget.** Each entity gets its own bounded bundle (see
`assemble_entity_context`): current full source + diff vs base are kept unconditionally
(they ARE the change under review), then 1-hop neighbor capsules and impact-flagged
exposition excerpts are added, nearest/most-relevant first, until an approximate token budget
(`DEFAULT_BUDGET`, reusing `tools.assemble_context.est_tokens` — the same chars/4
estimate, not a real tokenizer) is hit; anything left over is dropped with a visible
notice, exactly like `tools.assemble_context.assemble`'s own budget-trim step.
Exposition excerpts are additionally hard-capped per-file at `MAX_SCENE_EXCERPT_CHARS`
before the token budget even runs, so one long writeup can't crowd out every other
block by itself.

**One LLM invocation path.** Dispatch goes through `tools.orchestrate.make_runner`,
which itself delegates to `tools.capsules.run_llm_{cli,api}` — there is no second way to
call a model in this repo (the one-invocation-path rule). `cli` (the
Claude subscription) is the default; `--backend api` is opt-in and what CI uses.

**Output contract, parsed defensively.** The model must return a bare JSON array of
findings: `[{severity, entities, claim, rationale, suggested_override}]` (see
`SYSTEM_PROMPT` for the exact instructions given to it, including `suggested_override`'s
required shape — the `Finding.suggestion` payload, `tools.validate._conflict_suggestion`
— which the model is asked to emit directly rather than this module templating it,
since only the model can judge whether an `intentional_conflicts` waiver is the right
repair). `parse_findings` never raises: anything that isn't a clean JSON array of
well-shaped objects (markdown fences, prose preamble, a wrong key, a bad severity value,
...) degrades to exactly ONE advisory finding —
"canon-check output unparseable" — carrying a truncated copy of the raw text, never a
crash.

**Advisory, always.** Exit 0 regardless of findings; `--strict` (opt-in, local use only —
CI never sets it) exits 1 if any `high`-severity finding is present. A bad `--base` ref
is the one usage error that exits 1 unconditionally, matching `tools.pr_annotate` /
`tools.impact`'s identical precedent. No modified entities in the diff -> "nothing to
check", exit 0, zero LLM calls (checked before context assembly or runner construction,
so this is genuinely free, not just fast).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .assemble_context import est_tokens
from .capsules import capsule_body
from .common import ROOT
from .graph import build_graph, neighborhood
from .impact import build_impact
from .loader import Canon, load_canon
from .orchestrate import Runner, make_runner
from .pr_annotate import BaseRefError, DiffSummary, summarize_diff

# Same tier as tools.capsules.CAPSULE_MODEL (haiku-class): canon-check is bounded
# contradiction-spotting over a small assembled bundle, not open-ended reasoning or
# drafting. A separate constant, not an alias, so the two call sites' defaults can
# move independently later.
CANON_CHECK_MODEL = "claude-haiku-4-5"

# Approx token budget (tools.assemble_context.est_tokens: chars/4) for one entity's
# whole bundle (source + diff + neighbors + exposition excerpts). This module pays for one
# LLM call per modified entity, so the per-call bundle is deliberately smaller than
# assemble_context's own (human-session, single-entity) default of "unbounded unless
# --budget is passed" — a PR can touch several entities at once, and CI's --backend api
# run is metered. 3000 tokens comfortably fits a full entity file, a realistic diff, a
# handful of neighbor capsules, and a couple of exposition excerpts; override with --budget.
DEFAULT_BUDGET = 3000

# Cap on any one exposition excerpt before the token budget above even applies. A
# single file can be many times the whole per-entity budget on its own; this keeps one
# long writeup from silently starving every other block (neighbors, other files) in the
# budget-trim loop below.
MAX_SCENE_EXCERPT_CHARS = 800

VALID_SEVERITIES = ("high", "medium", "low")

SYSTEM_PROMPT = """\
You are the advisory canon-check pass for a mathematics research knowledge base. You \
are given one changed entity: its current full source (frontmatter + prose), the git \
diff that produced this change, capsules for its immediate (1-hop) neighbors, and \
excerpts of any expositions (writeups) that reference it. Your only job is to flag \
genuine CONTRADICTIONS the change introduces against that surrounding context — not \
style, not exposition quality, not missing detail, not typos.

A contradiction is: the changed entity's current source states or implies something a \
neighbor capsule or exposition excerpt directly disagrees with (for example: a \
statement's hypotheses weakened while a proof that uses it still needs the stronger \
form; a definition changed so a dependent statement's phrasing no longer parses; a \
status flipped to refuted while a writeup still invokes the result; two entities now \
asserting incompatible values for the same constant; a symbol redefined against the \
notation another entity declares). If you see none, say so with an empty list — do not \
invent a contradiction to have something to report.

Output ONLY a JSON array (no prose before or after, no markdown code fence), where each \
element has exactly these keys:

  severity: "high" | "medium" | "low"
  entities: a list of entity ids involved (the changed entity plus whichever \
neighbor/exposition-mentioned id(s) it conflicts with)
  claim: one sentence naming the specific claim in tension
  rationale: one or two sentences explaining the contradiction, citing what the \
source, diff, neighbor capsule, or exposition excerpt actually says
  suggested_override: either null, or — only when the disagreement is the kind that \
is legitimately *tracked* rather than fixed (two papers defining the same term \
incompatibly, a convention clash, competing accounts of a result) — a ready-to-paste \
YAML snippet in EXACTLY this shape (target is the OTHER entity's id, nature is a \
short quoted phrase, written from the changed entity's point of view):

intentional_conflicts:
  - target: <other-entity-id>
    nature: "<short description of the disagreement>"
    resolution_status: unresolved

No contradictions found -> output exactly: []
"""


@dataclass
class CanonCheckFinding:
    """One canon-check finding, exactly the model output contract's shape.

    `suggested_override` is model-written free text (see SYSTEM_PROMPT), not templated
    here — unlike `tools.validate._conflict_suggestion`, which is deterministic because
    the validator already knows which two ids and what nature triggered it; here only
    the model has judged whether a waiver is the right repair at all.
    """

    severity: str
    entities: list[str] = field(default_factory=list)
    claim: str = ""
    rationale: str = ""
    suggested_override: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "entities": list(self.entities),
            "claim": self.claim,
            "rationale": self.rationale,
            "suggested_override": self.suggested_override,
        }


# --- output parsing (defensive: never raises) ---------------------------------------

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```\s*$")


def _unparseable_finding(entity_id: str, raw: str) -> CanonCheckFinding:
    """The one advisory finding a malformed model response degrades to (never a crash)."""
    truncated = (raw or "").strip()[:500]
    return CanonCheckFinding(
        severity="low",
        entities=[entity_id],
        claim="canon-check output unparseable",
        rationale=f"Raw model output (truncated): {truncated!r}",
        suggested_override=None,
    )


def _parse_one(item) -> CanonCheckFinding | None:
    """One JSON element -> a CanonCheckFinding, or None if it doesn't match the contract."""
    if not isinstance(item, dict):
        return None
    severity = item.get("severity")
    entities = item.get("entities")
    claim = item.get("claim")
    rationale = item.get("rationale")
    suggested_override = item.get("suggested_override")
    if severity not in VALID_SEVERITIES:
        return None
    if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
        return None
    if not isinstance(claim, str) or not isinstance(rationale, str):
        return None
    if suggested_override is not None and not isinstance(suggested_override, str):
        return None
    return CanonCheckFinding(severity, list(entities), claim, rationale, suggested_override)


def parse_findings(raw: str, *, entity_id: str) -> list[CanonCheckFinding]:
    """Defensively parse one entity's model response into findings.

    Any failure mode — a markdown code fence, prose wrapped around the JSON, invalid
    JSON, a non-list top level, or any element missing/mistyping a required key —
    degrades to exactly one `_unparseable_finding`, never a crash and never a partial
    list of "the ones that happened to parse" (a response we can't fully trust is not
    one we selectively trust). An empty array `[]` is a valid, meaningful "no issues".
    """
    text = (raw or "").strip()
    stripped = _FENCE_RE.sub("", text).strip()
    start, end = stripped.find("["), stripped.rfind("]")
    candidate = stripped[start:end + 1] if start != -1 and end != -1 and end > start else stripped

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return [_unparseable_finding(entity_id, raw)]
    if not isinstance(data, list):
        return [_unparseable_finding(entity_id, raw)]

    findings: list[CanonCheckFinding] = []
    for item in data:
        parsed = _parse_one(item)
        if parsed is None:
            return [_unparseable_finding(entity_id, raw)]
        findings.append(parsed)
    return findings


# --- context assembly (bounded; see module docstring) --------------------------------

def _file_diff(path: str, base: str, *, root: Path) -> str:
    """`git diff base -- path`, run from root. Empty string on any git failure."""
    proc = subprocess.run(["git", "diff", base, "--", path], cwd=root,
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").rstrip()


def assemble_entity_context(
    eid: str,
    *,
    base: str,
    canon: Canon,
    impact_entry: dict[str, list[str]] | None,
    root: Path = ROOT,
    budget: int = DEFAULT_BUDGET,
    capsules_dir: Path | None = None,
) -> str:
    """Bounded, per-entity context bundle for one canon-check LLM call.

    Four ordered pieces (see module docstring): current full source and the diff vs
    `base` are kept unconditionally (blocks[0:2] — they ARE the change under review, the
    same "target always kept" discipline `tools.assemble_context.assemble` applies to its
    own block 0); 1-hop neighbor capsules and impact-flagged exposition excerpts are then
    added in that priority order until `budget` (approx tokens) is hit, with a trailing
    notice if anything had to be dropped. `capsules_dir` defaults to the real
    `derived/capsules/` under `root` (mirrors `tools.capsules.capsule_body`'s own
    default) — injectable so tests can point it at a fixture's capsules without
    touching the real repo's.
    """
    capsules_dir = Path(root) / "derived" / "capsules" if capsules_dir is None else capsules_dir
    relpath = str(canon.paths[eid].relative_to(root))

    source_block = (f"# === ENTITY {eid} (current full source) ===\n"
                    f"{canon.paths[eid].read_text().rstrip()}\n")
    diff_text = _file_diff(relpath, base, root=root)
    diff_block = (f"# === DIFF vs {base} ({relpath}) ===\n"
                  f"{diff_text if diff_text else '(no textual diff found)'}\n")

    g = build_graph(canon)
    dist = neighborhood(g, eid, 1)
    neighbor_ids = sorted((hop, nid) for nid, hop in dist.items() if nid != eid)

    optional_blocks: list[str] = []
    for hop, nid in neighbor_ids:
        text = capsule_body(nid, canon.entities.get(nid), capsules_dir=capsules_dir).rstrip()
        optional_blocks.append(f"# --- neighbor: {nid} [hop {hop}] ---\n{text}\n")

    for exp_path in sorted((impact_entry or {}).get("expositions", [])):
        try:
            text = (root / exp_path).read_text().strip()
        except OSError:
            continue
        if len(text) > MAX_SCENE_EXCERPT_CHARS:
            text = text[:MAX_SCENE_EXCERPT_CHARS] + "\n...[truncated]"
        optional_blocks.append(f"# --- exposition excerpt: {exp_path} ---\n{text}\n")

    kept = [source_block, diff_block]
    used = est_tokens(source_block) + est_tokens(diff_block)
    dropped = 0
    for text in optional_blocks:
        t = est_tokens(text)
        if used + t > budget:
            dropped += 1
            continue
        kept.append(text)
        used += t
    if dropped:
        kept.append(f"# [budget {budget} tok: dropped {dropped} lower-priority block(s)]\n")

    return "\n".join(kept)


# --- the check itself ------------------------------------------------------------------

def check_canon(
    base: str,
    *,
    root: Path = ROOT,
    canon: Canon | None = None,
    summary: DiffSummary | None = None,
    impact: dict[str, dict[str, list[str]]] | None = None,
    runner: Runner | None = None,
    backend: str = "cli",
    model: str = CANON_CHECK_MODEL,
    client=None,
    run=None,
    binary: str | None = None,
    budget: int = DEFAULT_BUDGET,
    capsules_dir: Path | None = None,
) -> dict[str, list[CanonCheckFinding]]:
    """One LLM call per modified entity; returns entity id -> its findings.

    Scope is `summary.changed_ids` only — see module docstring. Returns `{}` (zero LLM
    calls, `runner` never even constructed) when there are none, which is what makes
    the CLI's "no modified entities -> zero LLM calls" guarantee true rather than just
    fast. `runner` is the injected `(system, prompt) -> text` backend
    (`tools.orchestrate.Runner`); tests pass a fake directly and never touch a real
    backend. Production callers leave it `None` and get one built from
    `backend`/`model`/`client`/`run`/`binary` via `tools.orchestrate.make_runner` — the
    repo's one LLM invocation path. `capsules_dir` is forwarded to
    `assemble_entity_context` (see its docstring); `None` means "the real
    `derived/capsules/` under `root`".
    """
    canon = load_canon(root / "canon") if canon is None else canon
    summary = summarize_diff(base, root=root, canon=canon) if summary is None else summary
    if not summary.changed_ids:
        return {}

    impact = (build_impact(base, root=root, canon=canon, summary=summary)
             if impact is None else impact)
    runner = runner if runner is not None else make_runner(
        backend, model=model, client=client, run=run, binary=binary)

    results: dict[str, list[CanonCheckFinding]] = {}
    for eid in summary.changed_ids:
        context = assemble_entity_context(
            eid, base=base, canon=canon, impact_entry=impact.get(eid), root=root, budget=budget,
            capsules_dir=capsules_dir)
        user_prompt = f"# Entity under review: {eid}\n\n{context}"
        raw = runner(SYSTEM_PROMPT, user_prompt)
        results[eid] = parse_findings(raw, entity_id=eid)
    return results


# --- rendering ---------------------------------------------------------------------

def render_markdown(base: str, findings_by_entity: dict[str, list[CanonCheckFinding]]) -> str:
    lines = [f"## Canon check (base: `{base}`)", ""]
    if not findings_by_entity:
        lines.append("Nothing to check: no modified canon entities against this base.")
        return "\n".join(lines) + "\n"

    total = sum(len(fs) for fs in findings_by_entity.values())
    highs = sum(1 for fs in findings_by_entity.values() for f in fs if f.severity == "high")
    lines.append(
        f"**{len(findings_by_entity)} modified entit"
        f"{'y' if len(findings_by_entity) == 1 else 'ies'} checked, "
        f"{total} finding(s) ({highs} high).**"
    )
    lines.append("")

    for eid, findings in findings_by_entity.items():
        if not findings:
            lines.append(f"- `{eid}`: no issues found")
            continue
        lines.append(f"- `{eid}`:")
        for f in findings:
            lines.append(f"  - **{f.severity.upper()}** ({', '.join(f.entities)}) — {f.claim}")
            lines.append(f"    - {f.rationale}")
            if f.suggested_override:
                lines.append("    - suggested override:")
                lines.append("      ```yaml")
                for ln in f.suggested_override.rstrip().splitlines():
                    lines.append(f"      {ln}")
                lines.append("      ```")
    return "\n".join(lines) + "\n"


def to_json(findings_by_entity: dict[str, list[CanonCheckFinding]]) -> dict:
    """Stable `entity id -> [finding dict, ...]` shape, sorted keys, for `--json`."""
    return {eid: [f.to_dict() for f in fs] for eid, fs in sorted(findings_by_entity.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Advisory LLM canon-check: do this diff's modified entities now "
                    "contradict their 1-hop neighbors or referencing expositions?")
    parser.add_argument("--base", default="origin/main",
                        help="ref to diff canon/ against (default: origin/main)")
    parser.add_argument("--model", default=CANON_CHECK_MODEL,
                        help=f"model (default: {CANON_CHECK_MODEL})")
    parser.add_argument("--backend", choices=("cli", "api"), default="cli",
                        help="cli: `claude -p` on your subscription (default); api: metered API")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help=f"approx per-entity token budget (default: {DEFAULT_BUDGET})")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if any 'high' severity finding is present "
                            "(local use; CI never sets this)")
    args = parser.parse_args(argv)

    try:
        findings = check_canon(args.base, backend=args.backend, model=args.model,
                               budget=args.budget)
    except BaseRefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(to_json(findings), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_markdown(args.base, findings))

    if args.strict and any(f.severity == "high" for fs in findings.values() for f in fs):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

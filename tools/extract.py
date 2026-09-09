"""Extraction pipeline: raw material -> reviewable plan -> canon + an exposition.

    python -m tools.extract plan raw/<file>.md              # Extractor writes a plan
    python -m tools.extract check                           # structural gate on pending plans
    python -m tools.extract apply extraction/plans/<f>.md   # materialize approved findings

Canon is normally authored *into* the spine; this module is the on-ramp for material
written outside it — above all, your own writeups of papers (statements, definitions,
proofs, with citations; never the PDFs themselves), plus session transcripts and prose
drafts. A raw document lands in ``raw/``; the **Extractor** role
(``agents/extractor.md``) reads it and proposes an extraction plan into
``extraction/plans/``; a human triages every finding (flipping ``decision:`` and editing
any proposed field — in mathematics this triage IS the correctness gate: an
LLM-transcribed statement with subtly wrong hypotheses is poison, so review the
transcribed content, not just the name); ``apply`` then materializes the approved
findings and archives both files (raw -> ``raw/extracted/``, plan ->
``extraction/approved/``).

Where authority lives
---------------------
A plan is LLM-*proposed* but human-*ratified*: its power to mutate canon comes entirely
from the per-finding ``decision`` a human set, never from the model. That is why apply is
deliberately deterministic — no second agent "interprets" an approved plan. The plan
carries the complete artifacts (full frontmatter + body per created entity, explicit
field-level additions per update, the exact anchor phrases to wiki-link), so what the
human approved is byte-for-byte what lands. The only LLM work left after approval is
capsule generation, which already has its own pipeline (``tools.capsules``).

Three deterministic guarantees the human can rely on:

* **Staleness.** The plan embeds a hash of the raw file it was generated from (same
  philosophy as capsule freshness); apply refuses if the raw material changed since the
  plan was written. Re-run ``plan`` instead.
* **Verbatim prose.** The emitted exposition under ``expositions/<collection>/`` is the
  raw prose byte-for-byte, apart from exactly two mechanical transformations: approved
  anchor phrases become ``[[id]]`` wiki-links (first non-heading occurrence), and a
  provenance frontmatter block is prepended. Nothing rewrites the author's words.
* **Claimed terms.** Every emphasized or multi-word TitleCase term harvested from the
  raw material must be *claimed* — named in canon, covered by a finding, or ruled on
  via an auto-appended pending ``skip`` stub — so a recall miss by the Extractor
  becomes a visible triage decision instead of a silent omission. Named theorems
  ("Serre Duality", "the Weil Conjectures") surface through exactly this net (see the
  claim-check section below).

Raw frontmatter steers the one LLM step (``raw/_TEMPLATE.md`` documents the keys):
``hints:`` maps display names to existing entity ids, ``context:`` is free-form author
ground truth the text itself doesn't state (identities of unnamed figures, naming
conventions — findings drawn from it carry ``AUTHOR:``-prefixed evidence so triage can
tell the two apart), and ``kind: prose|notes`` declares what the material *is* (see
``RAW_KINDS``).

Relations and reciprocity: approving a relation of a *declared* type (see
``schemas/relations.py``) implies its inverse edge — apply adds the reciprocal relation
to the target entity mechanically, because the validator requires it and its shape is
fully determined (``relation_spec(...).inverse``). Undeclared types are written only
where the plan lists them.

Schema-less prose facts ride ``add.notes`` on a match/update finding: apply appends the
approved notes to the target entity's *body* as a date/source-stamped **unintegrated
block** (see tools.provenance for the format and lifecycle) — deterministic and
append-only, so no triaged fact is ever dropped for lack of a schema field. The block's
presence flags a still-owed integration pass; ``derived/indices/unintegrated.json`` is
the queue.

Plans and raw material are a *workflow* artifact class — human-owned working state, not
canon (they assert nothing once applied) and not derived (they are hand-edited by
design). So ``check`` here is a standalone convenience for triage, not part of the
``make check`` CI gate; the gate still owns the result, because apply ends by pointing
at it and CI would reject any apply that produced invalid canon.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import yaml

from schemas.registry import model_for_type
from schemas.relations import relation_spec

from . import capsules, provenance
from .capsules import source_hash
from .common import (
    AGENTS_DIR,
    CANON_DIR,
    CAPSULES_DIR,
    EXTRACTION_APPROVED_DIR,
    EXTRACTION_PLANS_DIR,
    EXPOSITIONS_DIR,
    RAW_EXTRACTED_DIR,
    ROOT,
)
from .editor import utc_timestamp
from .fmt import canonical_text
from .lifecycle import DIR_FOR_TYPE
from .loader import Canon, load_canon
from .orchestrate import ORCH_MODEL, Runner, make_runner
from .taxonomy import normalize_tags, unknown_tags

EXTRACTOR_PROMPT = AGENTS_DIR / "extractor.md"

# Extraction is whole-document reasoning (coreference, type judgement, canon matching),
# not summarization — orchestrator tier, like the editor. The completion is a full plan
# with entity frontmatter/bodies, so it needs a far bigger output budget than the
# capsule defaults make_runner would otherwise inherit.
EXTRACT_MODEL = ORCH_MODEL
EXTRACT_MAX_TOKENS = 16_000
EXTRACT_CLI_TIMEOUT = 900

ACTIONS = ("create", "match", "update", "skip")
DECISIONS = ("pending", "approved", "rejected")
ID_RE = re.compile(r"^[a-z]+\.[a-z0-9_]+$")

# What a raw file *is* changes what extraction means. "prose" is exposition-quality
# writing destined to become an expositions/ file (anchors matter — they become the
# wiki-links). "notes" is reference material — a paper writeup, research notes — and
# "transcript" is a transcribed working session or conversation: for both of those the
# facts are wanted but the text itself is not exposition prose, so no exposition is
# emitted and anchors are neither required nor checked. Declared in the raw file's
# frontmatter (`kind:`), recorded in the plan's frontmatter so triage and apply agree
# without re-reading raw. `transcript` is kept distinct from `notes` — same pipeline
# behaviour, but the provenance ("these claims were said in a session, not written in a
# paper writeup") is worth carrying downstream on the plan.
RAW_KINDS = ("prose", "notes", "transcript")

# The one behavioural axis `kind` controls: only these kinds emit a narrative scene.
# Every scene-vs-no-scene branch in the pipeline funnels through `kind_emits_scene` so
# adding a fourth kind is a one-line decision here, not a scatter of `== "notes"` checks.
EXPOSITION_KINDS = frozenset({"prose"})

# raw/ inbox bucket -> the kind a file in it defaults to when frontmatter omits `kind:`.
# The bucket is a convenience (drop a file in the obvious folder and it Just Works); an
# explicit `kind:` still overrides, and a file at raw/ root simply falls back to prose.
FOLDER_KIND = {"drafts": "prose", "papers": "notes", "sessions": "transcript"}


def kind_emits_exposition(kind: str) -> bool:
    """Whether material of this kind produces an expositions/ file on apply."""
    return kind in EXPOSITION_KINDS


def default_kind_for_raw(raw_path: Path, root: Path) -> str | None:
    """The kind implied by which raw/ bucket the file sits in, or None if unbucketed.

    Looks only at the first path component under ``raw/`` (so ``raw/notes/x.md`` and
    ``raw/notes/2026/x.md`` both imply ``notes``). Returns None for a file at ``raw/``
    root or outside ``raw/`` entirely — the caller then falls back to the historical
    default (prose), preserving pre-bucket behaviour.
    """
    try:
        rel = raw_path.resolve().relative_to((root / "raw").resolve())
    except ValueError:
        return None
    parts = rel.parts
    return FOLDER_KIND.get(parts[0]) if len(parts) > 1 else None

# Fenced YAML block = one finding. The plan body is Markdown for the human; the fences
# are the machine-readable part, so apply never has to parse prose.
_FINDING_RE = re.compile(r"^```yaml\s*\n(.*?)^```\s*$", re.M | re.S)

PLAN_TEMPLATE_CONTRACT = """\
# Output contract

Write ONE extraction plan body in exactly this structure (no preamble before the first
heading, no sign-off after the last finding):

## Summary
2-6 sentences: what the material is, what part of the project it bears on, and how it
relates to existing canon.

## Findings
One subsection per finding — a heading, then ONE fenced ```yaml block:

### <display label>
```yaml
action: create | match | update | skip
type: statement                 # create only: the entity type
id: thm.example_bound           # proposed (create) or existing (match/update); omit for skip
confidence: high | medium | low
evidence:
  - "short verbatim quote from the raw material"
candidates:                     # closest existing entities you considered, best first
  - {id: thm.other_bound, why: "same shape, but over a different base field"}
entity:                         # create only: full frontmatter EXCEPT id/type
  name: Example bound
  kind: theorem
  status: proved
  stated_in: src.example2024
  statement: |
    Let $X$ be ... . Then $\\dim X \\le n$.
add:                            # match/update only: values to append
  aliases: [the example bound]
  relations: []
  notes:                        # net-new schema-less facts; appended to the entity body
    - "One new fact the material states about this entity, in canon register."
set:                            # match/update only: empty scalar fields to fill
  stated_in: src.example2024
body: |                         # create only: 2-6 sentences of context prose; [[id]] links allowed
  ...
anchors: ["Example bound"]      # exact phrases in the raw prose to wiki-link in the exposition
decision: pending
```

Rules:
- Every key above is optional except action and decision; omit keys that don't apply
  rather than writing empty ones. `decision` is ALWAYS `pending`.
- TRANSCRIPTION FIDELITY IS YOUR PRIME DIRECTIVE. A `statement:` field must reproduce
  the material's hypotheses and conclusion exactly — transcribe, never paraphrase, and
  never silently strengthen, weaken, or drop a hypothesis. Keep the material's LaTeX as
  written; where the project's notation index names a symbol for the same object,
  prefer the project's symbol and say so in a note.
- Every factual field must be supported by the raw material; `evidence` quotes the
  support. Do not guess. Facts stated in an `# Author context (ground truth)` section
  count as support too — evidence them with a line prefixed `AUTHOR:` (a paraphrase of
  the context, not a prose quote) so triage can tell text-evidenced from author-stated.
- A theorem imported from a paper is `status: proved` with `stated_in:` naming the
  source; propose a separate `proof` create (a sketch body + `uses:` listing what the
  paper's argument invokes) when the material shows the proof's structure — that is
  what makes the dependency graph complete. Never `proved_here` for literature results.
- `anchors` must be verbatim substrings of the raw prose occurring outside `#` heading
  lines, and no two findings may claim the same anchor. Give every create/match/update
  finding at least one anchor when the raw prose refers to it — unless the task marks
  the material as notes/transcript, in which case no exposition will be emitted: omit
  `anchors`.
- Only propose relations the material evidences; a declared relation type implies its
  inverse, so state each relationship from one side only. Structural dependency
  (proves/uses/verifies/depends_on/instance_of) goes in the typed fields, NEVER in
  `relations:`.
- STICK TO THE SCHEMA'S FIELDS (schemas/entities.py; the validator rejects any other
  key). EVERY statement — theorem, lemma, proposition, corollary, conjecture,
  question — is `type: statement` with a `thm.` id; the flavor goes in `kind:`,
  which exists on statements ONLY (no other type takes a `kind`). Statement `status`
  is exactly one of conjectured | proved | proved_here | folklore | refuted — never
  invent another: a result the material asserts without showing a proof is
  `conjectured` (triage may promote it), not "stated". Per-type fields beyond the
  shared base (name/aliases/tags/summary/relations/...):
    definition:  statement, invokes, notation, defined_in
    statement:   kind, statement, invokes, status, stated_in, refuted_by
    proof:       proves (required), uses, strategy, completeness, source
    object:      instance_of, notation, constructed_in
    source:      authors, year, bibkey, venue, url — `name` IS the paper's title
    computation: path, language, covers, conclusion, verifies, depends_on (comp.* only)
    technique:   related
  Statements and definitions take NO `depends_on`.
- DRAW THE FORMULATION EDGES. Every statement or definition create lists in
  `invokes:` the definitions (and objects) its formulation is stated in terms of —
  the notions a reader must hold for the `statement:` field to parse, including ones
  you are proposing in this same plan. This is a different axis from argumentative
  dependency: what an argument NEEDS goes in a proof's `uses:`; what a claim is
  ABOUT goes in `invokes:`. A conjecture with no proof still invokes its vocabulary.
- CANON OUTLIVES THE MATERIAL. Entity `statement:`, `body:`, and note text must read
  as timeless, self-contained mathematics — a reader who has never seen the raw
  material loses nothing. Never write "this draft/paper", "the author", "the
  abstract", "Section 3", "above/below", or narrative references to other results by
  display name; cross-reference other entities ONLY as [[id]] links (including ids
  proposed elsewhere in this plan). Provenance belongs in typed fields (`stated_in`,
  `defined_in`, a proof's `source`), never woven into prose. Inside a transcribed
  `statement:`, document-deixis is not mathematical content: replace phrases like
  "the composition law below" or "(see Theorem 3.2)" with the [[id]] or neutral
  wording, and keep every hypothesis and conclusion verbatim otherwise.
- SIGNIFIERS ARE BOUND VARIABLES. A letter belongs to a context, not to an entity:
  the same $V$ may be a poset in one statement and a vector space in another, so
  every `statement:` must bind its own symbols in its hypotheses ("Let $(P,<)$ be a
  finite poset ...") rather than inheriting a source document's ambient conventions.
  `notation:` entries record the project's conventional signifier for a concept —
  the signified — with its scope spelled out in `notes` ("within a fixed ambient
  poset $P$"); they are defaults for prose, not ownership of a letter. Never mint an
  object entity just to reserve a symbol, and never treat a bare letter as globally
  bound to one meaning.
- `add.notes` is where every net-new schema-less fact about an EXISTING entity lands:
  one note per fact, a sentence or two, canon register. Do not restate what `add`/`set`
  already capture as typed fields. When unsure whether the entity already records a
  fact, include the note anyway — a duplicate is cheap to drop at integration; an
  omitted fact is lost. Notes exist ONLY under `add:` on match/update findings — never
  under `entity:` — because a create carries its facts woven into `body:` prose
  instead; a create finding with leftover facts needs a longer body, not a notes
  list."""


# --- plan parsing --------------------------------------------------------------

@dataclass
class Finding:
    """One triageable unit of an extraction plan: a single fenced YAML block."""

    index: int
    data: dict
    error: str | None = None   # YAML parse failure; a broken block is still a finding

    @property
    def action(self) -> str:
        return str(self.data.get("action", ""))

    @property
    def decision(self) -> str:
        return str(self.data.get("decision", ""))

    @property
    def id(self) -> str | None:
        return self.data.get("id")

    @property
    def type(self) -> str | None:
        return self.data.get("type")

    @property
    def entity(self) -> dict:
        return self.data.get("entity") or {}

    @property
    def add(self) -> dict:
        return self.data.get("add") or {}

    @property
    def sets(self) -> dict:
        return self.data.get("set") or {}

    @property
    def body(self) -> str:
        return self.data.get("body") or ""

    @property
    def anchors(self) -> list[str]:
        val = self.data.get("anchors") or []
        return val if isinstance(val, list) else [val]

    @property
    def label(self) -> str:
        return (self.id or self.entity.get("name") or self.data.get("term")
                or f"finding {self.index}")

    @property
    def notes(self) -> list[provenance.Note]:
        """The finding's `add.notes` normalized to Notes; garbage items are dropped.

        Shape enforcement lives in `_note_problems` (run by check AND apply before any
        write), so by the time apply reads this property a malformed item cannot occur;
        dropping rather than raising here keeps the property safe for check to call on
        a plan that is *about* to be refused.
        """
        out: list[provenance.Note] = []
        for item in self.add.get("notes") or []:
            if isinstance(item, str) and item.strip():
                out.append(provenance.Note(text=item.strip()))
            elif isinstance(item, dict) and str(item.get("text") or "").strip():
                out.append(provenance.Note(text=str(item["text"]).strip(),
                                           spoiler=bool(item.get("spoiler"))))
        return out


@dataclass
class Plan:
    """A parsed extraction plan: frontmatter, prose body, and its findings."""

    path: Path
    meta: dict
    body: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def kind(self) -> str:
        # Plans written before kinds existed have no `kind:` key; they were all prose.
        return str(self.meta.get("kind") or "prose")

    @property
    def tags(self) -> list[str]:
        """Material tags carried from the raw file (normalized). Empty if untagged."""
        return normalize_tags(self.meta.get("tags"))

    @property
    def structural_problems(self) -> list[str]:
        problems = []
        for key in ("source", "source_sha", "status", "collection", "title"):
            if not self.meta.get(key):
                problems.append(f"plan frontmatter missing {key!r}")
        if self.meta.get("status") not in ("pending", "applied"):
            problems.append(f"plan status must be pending|applied, got {self.meta.get('status')!r}")
        if self.kind not in RAW_KINDS:
            problems.append(f"plan kind must be {'|'.join(RAW_KINDS)}, got {self.meta.get('kind')!r}")
        if not self.findings:
            problems.append("plan contains no findings (no fenced ```yaml blocks)")
        for f in self.findings:
            if f.error:
                problems.append(f"{f.label}: unparseable YAML: {f.error}")
        return problems


def parse_findings(content: str) -> list[Finding]:
    """Every fenced YAML block in a plan body, parsed; broken blocks kept as errors."""
    findings = []
    for i, m in enumerate(_FINDING_RE.finditer(content), start=1):
        try:
            data = yaml.safe_load(m.group(1))
            if not isinstance(data, dict):
                findings.append(Finding(i, {}, error="block is not a YAML mapping"))
                continue
            findings.append(Finding(i, data))
        except yaml.YAMLError as exc:
            findings.append(Finding(i, {}, error=str(exc).splitlines()[0]))
    return findings


def parse_plan(path: Path) -> Plan:
    post = frontmatter.load(path)
    return Plan(path=Path(path), meta=dict(post.metadata), body=post.content,
                findings=parse_findings(post.content))


def _plan_text(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body.rstrip()}\n"


def _ensure_pending_decisions(body: str) -> tuple[str, int]:
    """Insert ``decision: pending`` into any finding block the model left without one.

    The output contract says every finding carries ``decision: pending``, but the one
    LLM call in the pipeline sometimes drops the line (observed: an entire plan's worth
    of ``create`` findings at once), leaving a freshly generated plan that fails
    ``extract check`` before a human has touched it. The untriaged state is not the
    model's to choose, so the fix is normalization here, not re-prompting: a missing
    key becomes ``pending`` deterministically. Blocks that already carry any decision
    — or that don't parse as a YAML mapping (structural_problems will surface those) —
    are left byte-identical: this fills a hole, it never overrides.
    """
    filled = 0

    def _fill(m: re.Match) -> str:
        nonlocal filled
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            return m.group(0)
        if not isinstance(data, dict) or "decision" in data:
            return m.group(0)
        filled += 1
        full = m.group(0)
        fence = full.rfind("```")
        return full[:fence] + "decision: pending\n" + full[fence:]

    return _FINDING_RE.sub(_fill, body), filled


def _scene_text(meta: dict, body: str) -> str:
    """An expositions/ file: YAML frontmatter (tags + provenance) then verbatim prose.

    Same shape as `_plan_text`; kept separate because the two carry different keys and
    reading them together would blur that a scene's frontmatter is a *published* record
    (tags for retrieval, where the prose came from) while a plan's is workflow state.
    `sort_keys=False` preserves the caller's deliberate key order (tags first).
    """
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.rstrip()}\n"


# --- finding validation ----------------------------------------------------------

def _anchor_span(prose: str, anchor: str) -> tuple[int, int] | None:
    """First occurrence of `anchor` in `prose` that is not on a `#` heading line."""
    start = 0
    while True:
        i = prose.find(anchor, start)
        if i == -1:
            return None
        line_start = prose.rfind("\n", 0, i) + 1
        line_end = prose.find("\n", line_start)
        line = prose[line_start: line_end if line_end != -1 else len(prose)]
        if not line.lstrip().startswith("#"):
            return (i, i + len(anchor))
        start = i + 1


def _merged_update_meta(current: dict, f: Finding) -> tuple[dict, list[str]]:
    """Apply a match/update finding's `add`/`set` to a copy of an entity's frontmatter.

    Returns (merged meta, problems). `add` appends missing values; `set` only fills a
    field that is currently empty — filling over a *different* existing value is a
    conflict the human must resolve in canon, not something apply may silently do.
    """
    meta = dict(current)
    problems: list[str] = []
    # `add` merges exactly two frontmatter fields: aliases and relations. `notes` is
    # legitimate but routes to the entity *body* as a stamped unintegrated block (see
    # tools.provenance), not to frontmatter — apply handles it after the merge. Anything
    # else the model invents (observed: `add.traits`, a field no schema declares) must
    # refuse loudly — the silent alternative is an approved finding that "applies" while
    # changing nothing, which reads as success to the human who triaged it.
    for key in f.add:
        if key not in ("aliases", "relations", "notes"):
            problems.append(
                f"{f.label}: add.{key} is not a mergeable field (only aliases and "
                f"relations merge; a schema-less fact belongs in add.notes)")
    aliases = list(meta.get("aliases") or [])
    for a in f.add.get("aliases") or []:
        if a not in aliases:
            aliases.append(a)
    if aliases:
        meta["aliases"] = aliases
    rels = list(meta.get("relations") or [])
    have = {(r.get("type"), r.get("target")) for r in rels if isinstance(r, dict)}
    for r in f.add.get("relations") or []:
        if not isinstance(r, dict):
            problems.append(f"{f.label}: add.relations entry is not a mapping: {r!r}")
            continue
        if (r.get("type"), r.get("target")) not in have:
            rels.append(r)
    if rels:
        meta["relations"] = rels
    for key, val in f.sets.items():
        existing = meta.get(key)
        if existing not in (None, "", [], val):
            problems.append(
                f"{f.label}: set.{key} would overwrite existing value {existing!r} "
                f"with {val!r}; resolve in canon instead"
            )
            continue
        meta[key] = val
    return meta, problems


def _note_problems(f: Finding) -> list[str]:
    """Shape errors in a finding's `add.notes` (see PLAN_TEMPLATE_CONTRACT).

    A note is a non-empty string or a `{text: ..., spoiler: bool}` mapping and nothing
    else — the strictness matters because apply renders notes into canon bodies
    deterministically: a shape it silently coerced would be a fact the human triaged in
    one form landing in another.
    """
    raw = f.add.get("notes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [f"{f.label}: add.notes must be a list, got {type(raw).__name__}"]
    problems = []
    for i, item in enumerate(raw, start=1):
        if isinstance(item, str):
            if not item.strip():
                problems.append(f"{f.label}: add.notes[{i}] is empty")
        elif isinstance(item, dict):
            if not str(item.get("text") or "").strip():
                problems.append(f"{f.label}: add.notes[{i}] mapping needs a non-empty "
                                f"`text`")
            extra = set(item) - {"text", "spoiler"}
            if extra:
                problems.append(f"{f.label}: add.notes[{i}] has unknown key(s) "
                                f"{', '.join(sorted(extra))} (only text/spoiler)")
            if "spoiler" in item and not isinstance(item["spoiler"], bool):
                problems.append(f"{f.label}: add.notes[{i}].spoiler must be a boolean")
        else:
            problems.append(f"{f.label}: add.notes[{i}] must be a string or a "
                            f"{{text, spoiler}} mapping, got {type(item).__name__}")
    return problems


def _relation_targets(f: Finding) -> list[str]:
    rels = (f.entity.get("relations") or []) + (f.add.get("relations") or [])
    return [r.get("target") for r in rels if isinstance(r, dict) and r.get("target")]


def finding_problems(f: Finding, *, canon: Canon, created_ids: set[str],
                     prose: str | None, claimed_anchors: dict[str, str]) -> list[str]:
    """Everything wrong with one finding, relative to the current canon.

    `created_ids` are the ids other findings in the same plan create, so intra-plan
    relations resolve. `prose` enables the anchor checks (pass None to skip them, e.g.
    when applying with --no-scene). `claimed_anchors` is shared mutable state across the
    plan's findings so a phrase claimed twice is reported exactly once, on the second
    claimant.
    """
    p: list[str] = []
    if f.error:
        return [f"{f.label}: unparseable YAML: {f.error}"]
    if f.action not in ACTIONS:
        p.append(f"{f.label}: unknown action {f.action!r} (expected one of {'/'.join(ACTIONS)})")
        return p
    if f.decision not in DECISIONS:
        p.append(f"{f.label}: unknown decision {f.decision!r}")
    if f.action == "skip":
        return p

    if not f.id or not ID_RE.match(f.id):
        p.append(f"{f.label}: missing or malformed id {f.id!r} (expected <prefix>.<slug>)")
        return p

    if f.action == "create":
        model = model_for_type(f.type)
        if model is None:
            p.append(f"{f.label}: unknown entity type {f.type!r}")
            return p
        prefix = f.id.split(".", 1)[0]
        if prefix != model.id_prefix:
            p.append(f"{f.label}: id prefix {prefix!r} does not match type "
                     f"{f.type!r} (expected {model.id_prefix!r})")
        if f.id in canon.entities:
            p.append(f"{f.label}: id already exists in canon — use match/update, not create")
        for reserved in ("id", "type"):
            if reserved in f.entity:
                p.append(f"{f.label}: entity block must not set {reserved!r}")
        if "notes" in f.entity:
            # Observed model failure: notes nested under entity: on a create. Caught
            # here by name because the generic schema error it would otherwise raise
            # (pydantic's extra_forbidden) doesn't tell a triager what to DO with the
            # stranded facts.
            p.append(f"{f.label}: entity.notes is not an entity field — a create's "
                     f"schema-less facts belong woven into `body:` (notes are for "
                     f"match/update, where no body exists to write)")
        if not f.entity.get("name"):
            p.append(f"{f.label}: entity.name is required for create")
        if f.add or f.sets:
            p.append(f"{f.label}: add/set are for match/update, not create")
        if not p:
            try:
                model(**{"id": f.id, "type": f.type, **f.entity})
            except Exception as exc:
                p.append(f"{f.label}: proposed entity fails the schema: {exc}")
    else:  # match / update
        if f.id not in canon.entities:
            p.append(f"{f.label}: no such entity {f.id} to {f.action}")
            return p
        if f.entity or f.body:
            p.append(f"{f.label}: entity/body are for create, not {f.action}")
        p.extend(_note_problems(f))
        if f.add or f.sets:
            current = dict(frontmatter.load(canon.paths[f.id]).metadata)
            merged, merge_problems = _merged_update_meta(current, f)
            p.extend(merge_problems)
            if not merge_problems:
                try:
                    model_for_type(current.get("type"))(**merged)
                except Exception as exc:
                    p.append(f"{f.label}: updated entity would fail the schema: {exc}")

    known = set(canon.entities) | created_ids
    for target in _relation_targets(f):
        if target not in known:
            p.append(f"{f.label}: relation target {target!r} is neither in canon nor "
                     f"created by this plan")

    if prose is not None:
        for anchor in f.anchors:
            if not isinstance(anchor, str) or not anchor:
                p.append(f"{f.label}: anchor must be a non-empty string, got {anchor!r}")
                continue
            other = claimed_anchors.get(anchor)
            if other and other != f.label:
                p.append(f"{f.label}: anchor {anchor!r} already claimed by {other}")
                continue
            claimed_anchors[anchor] = f.label
            if _anchor_span(prose, anchor) is None:
                p.append(f"{f.label}: anchor {anchor!r} does not occur in the raw prose "
                         f"outside headings")
    return p


# Fields whose *absence* on a create finding makes the schema materialize a value the
# triager never saw. Pydantic defaults are right for hand-authored canon — a file's
# author owns their omissions — but a plan is transcription under review: an Extractor
# omitting `completeness` lands a sketched proof as "complete", and omitting `status`
# quietly files a claim as conjectured. (type, field) -> what silence will mean.
_DEFAULTED_FIELD_ADVISORIES: dict[tuple[str, str], str] = {
    ("proof", "completeness"): 'defaults to "complete"',
    ("statement", "status"): 'defaults to "conjectured"',
}


def finding_advisories(f: Finding) -> list[str]:
    """Non-failing triage notes: schema defaults about to do silent epistemic work.

    Deliberately separate from finding_problems: problems gate both check and apply,
    while leaning on a default is legitimate — the triager just has to have *seen*
    it. check_plans prints these as `note` lines and ignores them for its exit code;
    apply never consults them at all.
    """
    if f.error or f.action != "create":
        return []
    notes: list[str] = []
    for (ftype, field), consequence in sorted(_DEFAULTED_FIELD_ADVISORIES.items()):
        if f.type == ftype and field not in f.entity:
            notes.append(f"{f.label}: `{field}` not set — {consequence}; state it "
                         f"explicitly so triage rules on it")
    return notes


# --- plan generation (the one LLM step) -----------------------------------------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _first_h1(prose: str) -> str | None:
    for line in prose.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def candidate_index(canon: Canon) -> str:
    """One line per live canon entity: the Extractor's whole view of what exists.

    This is the assemble_context trade-off applied to extraction: the model needs
    breadth (is this mention already an entity?) not depth, so it gets every id with
    name/aliases/summary — a few hundred tokens — instead of any full source file.
    """
    lines = []
    for eid in sorted(canon.entities):
        e = canon.entities[eid]
        if e.merged_into is not None:
            continue
        parts = [eid, e.type, e.name]
        if e.aliases:
            parts.append("aliases: " + ", ".join(e.aliases))
        if e.summary:
            parts.append(e.summary)
        lines.append(" | ".join(parts))
    return "\n".join(lines) or "(the canon is empty)"


def prematch(prose: str, canon: Canon) -> tuple[list[str], list[str]]:
    """Deterministic first pass: (entity ids named in the text, fuzzy near-misses).

    Cheap string evidence handed to the Extractor so exact hits are never missed and
    likely misspellings/variants are surfaced; the model still owns the judgement
    (coreference, unnamed entities) that strings cannot settle.
    """
    present: list[str] = []
    names: dict[str, str] = {}
    for eid, e in canon.entities.items():
        if e.merged_into is not None:
            continue
        for n in [e.name, *e.aliases]:
            if n:
                names[n.lower()] = eid
        if any(re.search(rf"\b{re.escape(n)}\b", prose, re.I) for n in [e.name, *e.aliases] if n):
            present.append(eid)
    near: list[str] = []
    for word in sorted(set(re.findall(r"\b[A-Z][a-z]{3,}\b", prose))):
        for cand in difflib.get_close_matches(word.lower(), list(names), n=1, cutoff=0.85):
            if word.lower() != cand:
                near.append(f"{word} ~ {names[cand]} ({cand})")
    return sorted(present), near


# --- claim check (deterministic recall net; no LLM) ----------------------------------
#
# The Extractor's match-over-create bias buys precision at the cost of recall, and a
# recall miss is *silent*: a finding never proposed leaves nothing to triage. Observed
# on a re-extraction of already-applied material: ~10 net-new creates surfaced on the
# second pass, and 9 of the 10 carried a typographic marker in the raw file (the
# author's habit of emphasizing coined terms — `*unremembering*`, `**Eukar River**`) or
# were multi-word TitleCase runs ("Third World War"). Those markers are a deterministic
# signal, so the claim check turns them into an enforced invariant: every harvested
# term must be *claimed* — named in canon, covered by some finding, or explicitly
# ruled via a `skip` stub the human triages like any other finding. A silent miss
# becomes a pending decision.
#
# Precision dial (documented limits, not bugs): single words are harvested only when
# emphasized (every sentence-initial capital would otherwise be a candidate), and
# TitleCase runs whose leading sentence-word trim leaves one word are dropped. Unmarked
# lowercase coinages ("forgotten armageddon") are invisible to string harvesting by
# construction — that residue belongs to an LLM audit pass, not this net.
#
# Three further dials, added after one notes-kind extraction cost its author 50+ junk
# skip rulings (the Artemis-arc plan): ALL-CAPS spans are process flags (CANON FIX
# NEEDED, EARMARK, FIGURE OUT), never coined names — this canon TitleCases its names —
# so `_term_ok` drops them from BOTH harvest paths (they arrive via the TitleCase
# regex too, since it matches all-caps words); a fully parenthesized span is an
# authorial aside (`*(fuller treatment deferred)*`), same fate; and *structural*
# emphasis — a bold span that IS its whole line (a pseudo-heading) or opens a line
# with a colon right after (a run-in label) — is layout, not coinage, so the emphasis
# harvester skips it. The
# recall net under the second dial: only the emphasis *signal* is suppressed — the
# line's text stays in the TitleCase harvest, so a name-shaped heading or label
# ("**Tuulim Resistance**: ...") still surfaces via that path, while a prose-shaped
# one ("**The rumours**: ...") drops. Everything else emphasized mid-sentence remains
# a signal by authoring convention: bold in raw/ is reserved for claim-bearing terms.

# Bold/italic asterisk spans, innermost-first so **x** is one bold term, not two
# italic fragments. Underscore emphasis is not harvested: the author's raw material
# uses asterisks, and `_x_` collides with snake_case identifiers in technical notes.
_EMPHASIS_RE = re.compile(r"\*\*([^*\n]+?)\*\*|\*([^*\n]+?)\*")

# One TitleCase run: capitalized words (unicode letters, so Lanfrisuïm parses whole)
# optionally joined through lowercase connectors ("Fall of Alexandria", "Tarfad &
# Chelkin"). At least two capitalized words — see the precision note above.
_TITLE_WORD = r"[A-Z][\w'’-]*"
_TITLE_CONN = r"(?:of|the|and|de|du|la|von|&)"
_TITLE_RUN_RE = re.compile(
    rf"\b{_TITLE_WORD}(?:[ \t]+(?:{_TITLE_CONN}[ \t]+)?{_TITLE_WORD})+")

# Sentence-lead words trimmed off the front of a TitleCase run ("When Artemis Rose" ->
# "Artemis Rose"). Lowercased membership test — function words and pronouns, never
# anything that could begin a real name apart from "the"/"a" (whose loss
# `_normalize_term` makes symmetric by stripping articles everywhere).
_CAP_LEAD = frozenset("""
    a an the and but or nor as if when while after before with from for to by at in on
    of so then there this that these those he she they it we you i his her their its
    is are was were be not no since among wherever whenever where whether why how what
    which who whom whose your our my both each every either neither during through
    against toward towards upon within without because although though however thus
    hence also even once still yet see like
    """.split())

# Rank/kind words trimmed the same way ("Duke Barukhon" is a mention of Barukhon, not
# a distinct thing to claim; "Duchy of Kenabuïm" is Kenabuïm's). Trimmed both when
# harvesting runs and when testing claim variants, so a title-wrapped mention of a
# known entity never stubs. Generic geography ("River", "Sea") is deliberately NOT
# here: "Ulfin Sea"-shaped names carry the kind word as part of the name.
_RANK_LEAD = frozenset("""
    duke duchess emperor empress king queen prince princess lord lady captain sergeant
    sir saint master duchy
    """.split())

# Emphasized spans that are prose stress, not coined terms ("Omega was *here*").
# Filters single-word emphasis only; any multi-word emphasized span passes.
_STRESS_WORDS = frozenset("""
    a an the and but or not no yes is are was were be been am do does did done has have
    had can cannot could would should will wont dont cant this that these those here
    there now then never always all any some none very too only own same such just
    more most less least
    """.split())

_TERM_MAX_WORDS = 6
_TERM_MAX_CHARS = 60
_SENTENCE_PUNCT = re.compile(r"[.;:!?]")


def _fold_marks(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def _normalize_term(term: str) -> str:
    """Comparison form of a term: diacritics folded, casefolded, articles and
    possessives stripped.

    Symmetric — applied to harvested terms, canon names, and finding text alike — so
    "The Severed" claims `Severed`, "Ridilak Mudd's" matches "Ridilak Mudd", and
    `Lanfrisuïm` matches "Lanfrisuim" however the plan happened to spell it.
    """
    t = _fold_marks(term).casefold()
    t = re.sub(r"['’]s\b", "", t)        # possessive, before apostrophes vanish
    t = re.sub(r"[*_`\"'’]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" \t-—–,()[]")
    return re.sub(r"^(?:the|an|a) ", "", t)


def _non_heading_prose(prose: str) -> str:
    """The raw prose minus `#` heading lines — headings name sections, not new things,
    and their capitalized titles would otherwise flood the TitleCase harvest."""
    return "\n".join(l for l in prose.splitlines() if not l.lstrip().startswith("#"))


def _term_ok(term: str) -> bool:
    words = term.split()
    if not words or len(words) > _TERM_MAX_WORDS or len(term) > _TERM_MAX_CHARS:
        return False
    if _SENTENCE_PUNCT.search(term):     # emphasis spanning a sentence, not a term
        return False
    if term.isupper():                   # ALL-CAPS = process flag, never a coined name
        return False
    if term.startswith("(") and term.endswith(")"):
        return False                     # a fully parenthesized span is an aside

    norm = _normalize_term(term)
    if len(norm) < 3 or norm.isdigit():
        return False
    if len(norm.split()) == 1 and norm in _STRESS_WORDS:
        return False
    return True


# A bullet or numbered-list marker — the only prefix a structural emphasis span may
# have on its line (see _is_structural_emphasis).
_BULLET_RE = re.compile(r"[-*+]|\d+[.)]")


def _is_structural_emphasis(text: str, start: int, end: int) -> bool:
    """Whether the emphasis span at [start, end) is doing layout work, not coining.

    Two shapes, both judged on the span's own line: the span IS the line (a bold
    pseudo-heading — the unfenced cousin of the `#` headings _non_heading_prose already
    excludes, optionally closed by a colon), or the span opens the line with a colon
    immediately after (a run-in label, `- **The rumours**: ...`). A leading bullet or
    number marker is allowed in both. Anything with real prose before or after the
    span on its line is a mid-sentence signal and stays harvested. Deliberately
    emphasis-path-only: callers still feed the line to the TitleCase harvester, which
    is what keeps a name-shaped heading recallable (see the dial notes above).
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    prefix = text[line_start:start].strip()
    if prefix and not _BULLET_RE.fullmatch(prefix):
        return False
    suffix = text[end:line_end].strip()
    return suffix == "" or suffix.startswith(":")


def harvest_terms(prose: str) -> list[str]:
    """Candidate coined terms in raw material, in order of first appearance.

    Two harvesters over non-heading lines: emphasized spans (bold/italic — the author's
    coined-term convention; structural spans excluded, see _is_structural_emphasis) and
    multi-word TitleCase runs (names emphasis missed, like "Third World War"). Display
    form is the first occurrence's text; duplicates dedupe on the normalized form.
    """
    text = _non_heading_prose(prose)
    folded = _fold_marks(text).casefold()
    seen: set[str] = set()
    out: list[str] = []

    def _take(raw_term: str, *, single_needs_repeat: bool = False) -> None:
        term = raw_term.strip()
        if not _term_ok(term):
            return
        norm = _normalize_term(term)
        if not norm or norm in seen:
            return
        if single_needs_repeat and " " not in norm and term[:1].islower():
            # A lowercase word emphasized once is usually prose stress ("was *here*");
            # a coined term ("*unremembering*") recurs. Requiring a second occurrence
            # is the dial that separates them — a coinage introduced once and never
            # used again is missed, by choice.
            if len(re.findall(rf"\b{re.escape(norm)}\b", folded)) < 2:
                return
        seen.add(norm)
        out.append(term)

    spans: list[tuple[int, str, bool]] = []
    for m in _EMPHASIS_RE.finditer(text):
        if _is_structural_emphasis(text, m.start(), m.end()):
            continue
        spans.append((m.start(), m.group(1) or m.group(2), True))
    # TitleCase runs are harvested with emphasis markers stripped so a run isn't
    # truncated at an asterisk; positions stay comparable enough for ordering.
    plain = _EMPHASIS_RE.sub(lambda m: m.group(1) or m.group(2), text)
    for m in _TITLE_RUN_RE.finditer(plain):
        words = m.group(0).split()
        while words and words[0].casefold() in (_CAP_LEAD | _RANK_LEAD):
            words = words[1:]
        if len(words) >= 2:
            spans.append((m.start(), " ".join(words), False))
    for _, term, from_emphasis in sorted(spans, key=lambda s: s[0]):
        _take(term, single_needs_repeat=from_emphasis)
    return out


# Splitting a compound term into independently claimable parts: coordination and
# grouping punctuation only, never "of" ("Fall of Alexandria" is one name).
_PART_SPLIT_RE = re.compile(r"\s+(?:and|&)\s+|[()/,]")


def _claim_variants(norm: str) -> list[str]:
    """A normalized term plus its progressively lead-trimmed forms ("emperor of
    lanfrisuim" -> "of lanfrisuim" -> "lanfrisuim"), so a rank- or article-wrapped
    mention of a known name is claimed by the name itself."""
    out = [norm]
    words = norm.split()
    while len(words) > 1 and words[0] in (_RANK_LEAD | {"of", "the", "a", "an"}):
        words = words[1:]
        out.append(" ".join(words))
    # Bare depluralization ("Elthars" -> "Elthar") — last word only, and only the
    # trailing -s form: enough for name plurals without a stemmer's false claims.
    for form in list(out):
        if form.endswith("s") and not form.endswith("ss") and len(form) > 4:
            out.append(form[:-1])
    return out


def unclaimed_terms(prose: str, findings: list[Finding], canon: Canon) -> list[str]:
    """Harvested terms no finding covers and no canon entity is named by.

    A term is claimed by canon on an exact normalized match against any live entity's
    name, alias, or id slug (exact, not substring — "Moqinum Zul" must not claim "New
    Moqinum Zul"). It is claimed by the plan if it appears anywhere in a finding's
    YAML — evidence, name, body, notes, a stub's own `term:` — or in a finding id's
    slug ("loc.eukar_river" claims "Eukar River"); substring there, because findings
    quote prose around their subject. A compound ("Dheltar and Grempyr", "Barukh
    (Duke Barukhon)") is claimed when every substantive part is — but one unclaimed
    part ("Mann and Umirn the Divine Twins") keeps the whole term on the list.
    """
    canon_names: set[str] = set()
    for eid, e in canon.entities.items():
        if e.merged_into is not None:
            continue
        for n in [e.name, *e.aliases, eid.split(".", 1)[1].replace("_", " ")]:
            if not n:
                continue
            canon_names.add(_normalize_term(n))
            # A compound canon name claims its conjuncts too — "Mann and Umirn" must
            # claim a bare mention of *Mann* (the entity exists; the check would
            # otherwise misreport it as missing). Symmetric with the term-side
            # part-split below; short/function-word parts stay unclaimable so
            # "Ithikon (River / Delta / Valley)" never claims a stray "River".
            for part in _PART_SPLIT_RE.split(n):
                pn = _normalize_term(part)
                if len(pn) >= 3 and " " not in pn and pn not in (_CAP_LEAD | _STRESS_WORDS):
                    canon_names.add(pn)
                elif " " in pn:
                    canon_names.add(pn)
    claimed_blobs: list[str] = []
    for f in findings:
        blob = yaml.safe_dump(f.data, allow_unicode=True) if f.data else ""
        if f.id:
            blob += "\n" + f.id.replace(".", " ").replace("_", " ")
        claimed_blobs.append(_normalize_term(blob.replace("\n", " ")))
    claimed_text = " | ".join(claimed_blobs)

    def _atom_claimed(term: str) -> bool:
        for v in _claim_variants(_normalize_term(term)):
            if v in canon_names or (len(v) >= 3 and v in claimed_text):
                return True
        return False

    def _claimed(term: str) -> bool:
        if _atom_claimed(term):
            return True
        parts = [p.strip() for p in _PART_SPLIT_RE.split(term)]
        substantive = [p for p in parts if p and _term_ok(p)]
        return len(parts) > 1 and bool(substantive) and \
            all(_atom_claimed(p) for p in substantive)

    return [t for t in harvest_terms(prose) if not _claimed(t)]


CLAIM_STUB_EVIDENCE = ("CLAIM-CHECK: emphasized or TitleCase term with no covering "
                       "finding; approve this skip, or replace it with a "
                       "create/match/update finding.")


def claim_stub(term: str) -> str:
    """A pending `skip` finding for one unclaimed term.

    Riding the existing decision machinery is the whole point: `extract check` refuses
    an untriaged plan, so the human must rule on every harvested term — approving the
    skip records "seen, not an entity" in the audit trail, where deleting a warning
    would record nothing.
    """
    quoted = yaml.safe_dump({"term": term}, allow_unicode=True).strip()
    return (f"\n### {term} — unclaimed term\n"
            f"```yaml\naction: skip\n{quoted}\nconfidence: low\n"
            f"evidence:\n  - \"{CLAIM_STUB_EVIDENCE}\"\ndecision: pending\n```\n")


def append_claim_stubs(body: str, unclaimed: list[str]) -> str:
    if not unclaimed:
        return body
    header = ("\n\n## Unclaimed terms (claim check)\n\n"
              "Deterministic recall net: these emphasized/TitleCase terms from the raw "
              "material are covered by no finding above and name no existing canon "
              "entity. Each needs a ruling like any finding — approve the skip, or "
              "replace it with a real create/match/update.\n")
    return body.rstrip() + header + "".join(claim_stub(t) for t in unclaimed)


def make_plan(
    raw_file: str,
    *,
    runner: Runner,
    canon: Canon | None = None,
    root: Path = ROOT,
    plans_dir: Path = EXTRACTION_PLANS_DIR,
    role_prompt_path: Path = EXTRACTOR_PROMPT,
    force: bool = False,
    now=None,
) -> Path | None:
    """Run the Extractor over one raw file and write its plan. Returns the plan path.

    `runner` is the injected `(system, prompt) -> text` backend — the one LLM call in
    the pipeline. Everything else here is deterministic scaffolding: hint parsing, the
    candidate index, the string pre-match, frontmatter, and an immediate advisory
    re-parse of what the model produced (warnings only — the human is about to read the
    plan anyway; `apply` is where problems become refusals).
    """
    root = Path(root)
    raw_path = Path(raw_file) if Path(raw_file).is_absolute() else root / raw_file
    if not raw_path.exists():
        print(f"no such raw file: {raw_path}")
        return None
    if raw_path.name.startswith("_"):
        print(f"refusing to plan {raw_path.name}: a leading underscore marks a "
              "template/scratch file, not raw material (copy it to a new name first)")
        return None

    if canon is None:
        canon = load_canon()
    if canon.errors:
        print("Refusing to run: canon has load/validation errors. Run validate first.")
        for path, msg in canon.errors:
            print(f"  {path}: {msg}")
        return None

    plans_dir = Path(plans_dir)
    dest = plans_dir / f"{raw_path.stem}.md"
    if dest.exists() and not force:
        print(f"plan already exists: {dest} (triage it, or re-run with --force to overwrite)")
        return None

    post = frontmatter.load(raw_path)
    hints: dict = post.metadata.get("hints") or {}
    context = str(post.metadata.get("context") or "").strip()

    # Kind: an explicit `kind:` always wins; otherwise the raw/ bucket the file sits in
    # implies it (drafts->prose, notes->notes, transcripts->transcript), falling back to
    # prose for an unbucketed file. A `kind:` that disagrees with its bucket is allowed
    # but flagged — usually a misfiled draft, occasionally deliberate.
    pre_warnings: list[str] = []
    folder_kind = default_kind_for_raw(raw_path, root)
    declared_kind = post.metadata.get("kind")
    kind = str(declared_kind or folder_kind or "prose")
    if kind not in RAW_KINDS:
        print(f"unknown kind {kind!r} in {raw_path.name} frontmatter "
              f"(expected {'|'.join(RAW_KINDS)})")
        return None
    if declared_kind and folder_kind and str(declared_kind) != folder_kind:
        pre_warnings.append(
            f"kind {str(declared_kind)!r} disagrees with the raw/ bucket this file is in "
            f"(which implies {folder_kind!r}) — is it filed in the right folder?")

    # Tags: a controlled vocabulary (schemas-free workflow metadata) recorded on the plan
    # and threaded onto the emitted scene. Unknown tags are advisory only.
    tags = normalize_tags(post.metadata.get("tags"))
    for t in unknown_tags(tags):
        pre_warnings.append(f"tag {t!r} is not in the material-tag vocabulary "
                            "(tools/taxonomy.py) — kept, but consider a canonical tag")

    prose = post.content
    collection = post.metadata.get("collection") or _slugify(raw_path.stem)
    title = post.metadata.get("title") or _first_h1(prose) or raw_path.stem

    present, near = prematch(prose, canon)
    task = ("# Task\nRead the raw material below and produce an extraction plan per "
            "your output contract.")
    if not kind_emits_exposition(kind):
        source_desc = {
            "notes": "reference NOTES (a paper writeup, research notes, technical "
                     "background)",
            "transcript": "a TRANSCRIPT of a working session or conversation — treat "
                          "what a speaker asserts as a candidate fact, but stay alert "
                          "that speakers may speculate, misremember, or think aloud",
        }.get(kind, f"material of kind {kind!r}")
        task += (f"\nThis material is {source_desc}, not exposition prose: no "
                 "exposition will be emitted from it, so omit `anchors` from every "
                 "finding. Extract the mathematical facts it states; ignore drafting "
                 "chatter about what the author might do.")
        # The exposition-less-kind exhaustiveness push. For prose, the exposition
        # carries the full text and entity findings distill identity — compression is
        # correct there. Here there is no exposition: a fact not carried by a finding
        # is not deferred, it is LOST on apply (the raw file just archives). Inherited
        # tuning from the ancestor spine, where under-capture on a notes plan cost
        # real material.
        task += (
            "\nBecause no exposition is emitted, your findings are the ONLY landing "
            "surface: any fact you do not carry in a finding is lost, not deferred. "
            "Be exhaustive per entity, not summary-minded. Every definition, theorem, "
            "lemma, and proposition the material states deserves its own finding with "
            "the statement transcribed; every proof whose structure is visible "
            "deserves a proof finding with `uses:`; every case-check or experiment a "
            "computation finding. For every entity you match or update, carry EVERY "
            "distinct fact the material states about it as its own `add.notes` entry, "
            "in the material's order — and a fact belongs on every entity it is about "
            "(a bound relates the statement AND the objects it bounds). Work section "
            "by section, and before finishing re-scan the material for sections that "
            "produced no finding: each must be either covered or genuinely "
            "canon-irrelevant. Author flag lines (FIX NEEDED, CHECK THIS, and similar "
            "ALL-CAPS markers) are directives: always produce a finding covering the "
            "flagged content — the marker itself is process markup, never a term to "
            "extract. Prefer over-capture: a redundant note is cheap to reject at "
            "triage; an omitted fact is silently lost.")
    user_parts = [
        task,
        f"# Canon candidate index\n{candidate_index(canon)}",
    ]
    if hints:
        rendered = "\n".join(f"- {name!r} is {eid}" for name, eid in hints.items())
        user_parts.append(f"# Author hints (ground truth)\n{rendered}")
    if context:
        user_parts.append(f"# Author context (ground truth)\n{context}")
    if present:
        user_parts.append("# Canon entities named verbatim in this text\n" +
                          "\n".join(present))
    if near:
        user_parts.append("# Fuzzy near-matches (verify: variant spelling or new entity?)\n" +
                          "\n".join(near))
    user_parts.append(f"# Raw material\n{prose}")

    system = f"{Path(role_prompt_path).read_text()}\n\n{PLAN_TEMPLATE_CONTRACT}"
    body = runner(system, "\n\n".join(user_parts))
    body, filled = _ensure_pending_decisions(body)
    if filled:
        pre_warnings.append(f"model omitted `decision:` on {filled} finding(s); "
                            "normalized to pending")

    # Claim check: every harvested term the model's findings don't cover becomes a
    # pending skip stub the human must rule on — the recall complement of the plan's
    # precision-biased matching (see the claim-check section above).
    unclaimed = unclaimed_terms(prose, parse_findings(body), canon)
    if unclaimed:
        body = append_claim_stubs(body, unclaimed)
        pre_warnings.append(
            f"claim check: {len(unclaimed)} unclaimed term(s) appended as pending "
            f"skip stub(s): {', '.join(unclaimed)}")

    try:
        source_rel = raw_path.relative_to(root).as_posix()
    except ValueError:
        source_rel = str(raw_path)
    meta = {
        "source": source_rel,
        "source_sha": source_hash(raw_path.read_bytes()),
        "created": utc_timestamp(now),
        "status": "pending",
        "kind": kind,
        "tags": tags,
        "collection": collection,
        "title": title,
    }
    plans_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(_plan_text(meta, f"# Extraction plan: {source_rel}\n\n{body.strip()}"))

    plan = parse_plan(dest)
    warnings = pre_warnings + plan.structural_problems
    claimed: dict[str, str] = {}
    created_ids = {f.id for f in plan.findings if f.action == "create" and f.id}
    for f in plan.findings:
        warnings += finding_problems(f, canon=canon, created_ids=created_ids,
                                     prose=prose if kind_emits_exposition(kind) else None,
                                     claimed_anchors=claimed)
    for w in warnings:
        print(f"WARN  {w}", file=sys.stderr)
    print(f"wrote {dest} ({len(plan.findings)} finding(s), {len(warnings)} warning(s)). "
          f"Triage every finding's decision, then `make extract-apply PLAN={dest}`.")
    return dest


# --- apply (deterministic; no LLM) ------------------------------------------------

def _next_scene_number(scenes_dir: Path) -> int:
    nums = [int(m.group(1)) for p in scenes_dir.glob("*.md")
            if (m := re.match(r"(\d+)-", p.name))]
    return max(nums, default=0) + 1


def emit_exposition(prose: str, *, collection: str, title: str, links: list[tuple[str, str]],
               expositions_dir: Path, meta: dict) -> Path:
    """Write the raw prose as a scene: `meta` as frontmatter, then the prose verbatim
    except anchors -> ``[[id]]``.

    Spans are located on the pristine prose and spliced back-to-front, so earlier
    replacements can never shift or corrupt later ones and an inserted id can never
    itself be matched as an anchor. Overlaps and missing anchors were refused during
    validation; hitting one here is a bug, hence the hard error. `meta` carries the
    scene's tags and provenance (extracted_from / source_sha / plan) — the successor to
    the old HTML-comment marker, now machine-readable frontmatter (see `_scene_text`).
    """
    spans: list[tuple[int, int, str]] = []
    for anchor, eid in links:
        span = _anchor_span(prose, anchor)
        if span is None:
            raise ValueError(f"anchor {anchor!r} not found in prose")
        spans.append((*span, eid))
    spans.sort()
    for (_, prev_end, _), (start, _, _) in zip(spans, spans[1:]):
        if start < prev_end:
            raise ValueError("overlapping anchor spans")
    text = prose
    for start, end, eid in reversed(spans):
        text = f"{text[:start]}[[{eid}]]{text[end:]}"

    coll_dir = Path(expositions_dir) / collection
    coll_dir.mkdir(parents=True, exist_ok=True)
    dest = coll_dir / f"{_next_scene_number(coll_dir):03d}-{_slugify(title)}.md"
    dest.write_text(_scene_text(meta, text.strip()))
    return dest


def apply_plan(
    plan_file: str,
    *,
    scene: bool = True,
    derive: bool = True,
    root: Path = ROOT,
    canon_dir: Path = CANON_DIR,
    expositions_dir: Path = EXPOSITIONS_DIR,
    capsules_dir: Path = CAPSULES_DIR,
    raw_extracted_dir: Path = RAW_EXTRACTED_DIR,
    approved_dir: Path = EXTRACTION_APPROVED_DIR,
    now=None,
) -> int:
    """Materialize a fully triaged plan. Validates everything, then mutates.

    Two-phase by design: every refusal fires before the first write, so a rejected
    apply leaves the tree untouched. `derive=False` skips the repo-global post-steps
    (placeholder capsules + tools.build) for tests that run against injected dirs.
    """
    root = Path(root)
    plan_path = Path(plan_file) if Path(plan_file).is_absolute() else root / plan_file
    if not plan_path.exists():
        print(f"no such plan: {plan_path}")
        return 1
    plan = parse_plan(plan_path)
    if not kind_emits_exposition(plan.kind):
        # Notes/transcripts never become a scene, whatever the CLI said — the kind was
        # fixed when the raw file was planned, not per-apply.
        scene = False

    problems = plan.structural_problems
    if plan.meta.get("status") == "applied":
        problems.append("plan is already applied")
    raw_path = root / str(plan.meta.get("source", ""))
    if not raw_path.exists():
        problems.append(f"raw source missing: {plan.meta.get('source')!r} "
                        "(was it already extracted, or moved?)")
    elif plan.meta.get("source_sha") and \
            source_hash(raw_path.read_bytes()) != plan.meta["source_sha"]:
        problems.append("stale plan: the raw material changed since this plan was "
                        "written — regenerate with `plan --force`")
    undecided = [f.label for f in plan.findings if f.decision == "pending"]
    if undecided:
        problems.append("undecided finding(s): " + ", ".join(undecided) +
                        " — triage every finding before apply")
    if problems:
        for pr in problems:
            print(f"ERROR {pr}")
        return 1

    canon = load_canon(canon_dir)
    if canon.errors:
        print("Refusing to apply: canon has load/validation errors. Run validate first.")
        for path, msg in canon.errors:
            print(f"  {path}: {msg}")
        return 1

    approved = [f for f in plan.findings if f.decision == "approved" and f.action != "skip"]
    created_ids = {f.id for f in approved if f.action == "create"}
    prose = frontmatter.load(raw_path).content

    problems = []
    claimed: dict[str, str] = {}
    for f in approved:
        problems += finding_problems(f, canon=canon, created_ids=created_ids,
                                     prose=prose if scene else None,
                                     claimed_anchors=claimed)
    raw_dest = Path(raw_extracted_dir) / raw_path.name
    plan_dest = Path(approved_dir) / plan_path.name
    for dest in (raw_dest, plan_dest):
        if dest.exists():
            problems.append(f"destination already exists: {dest}")
    if scene:
        links = [(a, f.id) for f in approved for a in f.anchors]
        if not links:
            problems.append("scene emission is on but no approved finding has anchors; "
                            "add anchors or pass --no-scene")
    if problems:
        for pr in problems:
            print(f"ERROR {pr}")
        return 1

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return str(p)

    # -- write phase -------------------------------------------------------------
    touched: dict[str, Path] = {}
    updated_ids: list[str] = []
    for f in approved:
        if f.action == "create":
            meta = {"id": f.id, "type": f.type, **f.entity}
            out_dir = Path(canon_dir) / DIR_FOR_TYPE.get(f.type, f"{f.type}s")
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{f.id.split('.', 1)[1]}.md"
            path.write_text(canonical_text(f.type, meta, f.body.strip()))
            touched[f.id] = path
            print(f"  created {f.id}")
        elif set(f.add) - {"notes"} or f.sets:
            # notes are excluded here: a notes-only finding changes no frontmatter, so
            # writing (and announcing) a merge for it would misreport what happened —
            # its entire effect is the body append below.
            path = canon.paths[f.id]
            post = frontmatter.load(path)
            merged, _ = _merged_update_meta(dict(post.metadata), f)
            path.write_text(canonical_text(merged.get("type"), merged, post.content))
            touched[f.id] = path
            updated_ids.append(f.id)
            print(f"  updated {f.id}")

    # Schema-less facts land as ONE stamped unintegrated block per entity per apply
    # (findings coalesce; see tools.provenance for the format and its lifecycle). The
    # stamp cites the ARCHIVED locations (raw/extracted/, extraction/approved/) because
    # its audience — the later integration pass — reads it after apply has moved both
    # files there.
    pending_notes: dict[str, list[provenance.Note]] = {}
    for f in approved:
        if f.action in ("match", "update") and f.notes:
            pending_notes.setdefault(f.id, []).extend(f.notes)
    stamp = provenance.stamp_date(now)
    for eid in sorted(pending_notes):
        path = canon.paths[eid]
        post = frontmatter.load(path)
        block = provenance.render_block(pending_notes[eid], date=stamp,
                                        source=_rel(raw_dest), plan=_rel(plan_dest))
        path.write_text(canonical_text(post.metadata.get("type"), dict(post.metadata),
                                       provenance.append_block(post.content, block)))
        touched[eid] = path
        if eid not in updated_ids:
            updated_ids.append(eid)
        print(f"  appended {len(pending_notes[eid])} unintegrated note(s) to {eid}")

    added = _ensure_reciprocity(touched, canon)
    for src, rel_type, target in added:
        print(f"  added reciprocal {rel_type} edge: {target} -> {src}")

    scene_path = None
    if scene:
        # Provenance moves into the scene's frontmatter (tags first, if any): where the
        # prose came from, the raw source hash, and the plan that authorized it.
        scene_meta: dict = {}
        if plan.tags:
            scene_meta["tags"] = plan.tags
        scene_meta.update({
            "extracted_from": _rel(raw_dest),
            "source_sha": plan.meta["source_sha"],
            "plan": _rel(plan_dest),
        })
        scene_path = emit_exposition(prose, collection=str(plan.meta["collection"]),
                                title=str(plan.meta["title"]), links=links,
                                expositions_dir=expositions_dir, meta=scene_meta)
        print(f"  wrote exposition {_rel(scene_path)}")

    raw_dest.parent.mkdir(parents=True, exist_ok=True)
    raw_path.rename(raw_dest)
    new_meta = {
        **plan.meta,
        "status": "applied",
        "applied": utc_timestamp(now),
        "extracted_to": _rel(raw_dest),
        "created_entities": sorted(created_ids),
        "updated_entities": sorted(set(updated_ids)),
    }
    if scene_path is not None:
        new_meta["exposition"] = _rel(scene_path)
    plan_dest.parent.mkdir(parents=True, exist_ok=True)
    plan_dest.write_text(_plan_text(new_meta, plan.body))
    plan_path.unlink()
    print(f"  archived plan -> {_rel(plan_dest)}; raw -> {_rel(raw_dest)}")

    if derive:
        canon2 = load_canon(canon_dir)
        if canon2.errors:
            print("apply produced a canon that fails to load — this is a bug in the "
                  "plan validator; run `make validate`:")
            for path, msg in canon2.errors:
                print(f"  {path}: {msg}")
            return 1
        capsules.bootstrap_capsules(canon2, out_dir=capsules_dir)
        from . import build as build_mod
        rc = build_mod.build()
        if rc:
            return rc

    if created_ids:
        only = " ".join(f"--only {eid}" for eid in sorted(created_ids))
        print(f"Next: `python -m tools.capsules {only}` to replace the placeholder "
              f"capsule(s), then `make check`.")
    else:
        print("Next: `make check`.")
    return 0


def _ensure_reciprocity(touched: dict[str, Path], canon: Canon) -> list[tuple[str, str, str]]:
    """Add the inverse edge for every declared-type relation on the touched entities.

    Purely mechanical completion of what the human approved: the validator *requires*
    these edges and `relation_spec` fully determines them, so writing them here keeps
    "approved plan" and "green gate" the same thing. Idempotent — an edge that already
    exists is never duplicated. Returns (source id, inverse type, target id) per edge
    actually added.
    """
    added: list[tuple[str, str, str]] = []

    def path_for(eid: str) -> Path | None:
        return touched.get(eid) or canon.paths.get(eid)

    for src_id, src_path in list(touched.items()):
        src_meta = dict(frontmatter.load(src_path).metadata)
        for rel in src_meta.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            spec = relation_spec(rel.get("type", ""))
            if spec is None:
                continue
            target_path = path_for(rel.get("target"))
            if target_path is None:
                continue  # dangling target; validation already refused this upstream
            post = frontmatter.load(target_path)
            meta = dict(post.metadata)
            rels = list(meta.get("relations") or [])
            if any(isinstance(r, dict) and r.get("type") == spec.inverse
                   and r.get("target") == src_id for r in rels):
                continue
            rels.append({"type": spec.inverse, "target": src_id})
            meta["relations"] = rels
            target_path.write_text(canonical_text(meta.get("type"), meta, post.content))
            added.append((src_id, spec.inverse, rel.get("target")))
    return added


# --- check (a gate leg: make check + CI, and the standalone triage aid) -------------

def check_plans(*, plans_dir: Path = EXTRACTION_PLANS_DIR, root: Path = ROOT,
                canon: Canon | None = None) -> int:
    """Structurally validate every pending plan; report all problems at once.

    Runs as a leg of `make check`/CI (as well as standalone): a committed plan is a
    repo artifact, so a schema-invalid proposal or a plan gone stale against its raw
    source should fail here, not at apply time weeks later. Two output tiers,
    deliberately distinct: `FAIL` problems drive the exit code, while `note`
    advisories (finding_advisories) never do — they exist so triage sees what an
    omitted defaulted field will silently materialize as, and a plan that leans on
    defaults on purpose still gates green. Plans parked on purpose belong in
    extraction/archived/, which this function never reads.
    """
    plans_dir = Path(plans_dir)
    paths = sorted(plans_dir.glob("*.md")) if plans_dir.exists() else []
    if not paths:
        print("no pending plans.")
        return 0
    if canon is None:
        canon = load_canon()
    total = 0
    for path in paths:
        plan = parse_plan(path)
        problems = plan.structural_problems
        raw_path = Path(root) / str(plan.meta.get("source", ""))
        prose = None
        if not raw_path.exists():
            problems.append(f"raw source missing: {plan.meta.get('source')!r}")
        else:
            prose = frontmatter.load(raw_path).content
            if plan.meta.get("source_sha") and \
                    source_hash(raw_path.read_bytes()) != plan.meta["source_sha"]:
                problems.append(
                    "stale: raw material changed since the plan was written — "
                    "re-plan, or move a deliberately parked plan to extraction/archived/")
            # Claim check re-runs against the plan as triaged: a stub the human deleted
            # (instead of ruling on) resurfaces here, as does any term left uncovered in
            # a plan written before the claim check existed or authored by hand.
            for term in unclaimed_terms(prose, plan.findings, canon):
                problems.append(
                    f"unclaimed term {term!r}: no finding covers it — add a skip "
                    f"finding (or a create/match/update)")
        if not kind_emits_exposition(plan.kind):
            prose = None  # notes/transcripts never emit a scene, so anchors aren't checked
        created_ids = {f.id for f in plan.findings if f.action == "create" and f.id}
        claimed: dict[str, str] = {}
        advisories: list[str] = []
        for f in plan.findings:
            problems += finding_problems(f, canon=canon, created_ids=created_ids,
                                         prose=prose, claimed_anchors=claimed)
            advisories += finding_advisories(f)
        undecided = sum(1 for f in plan.findings if f.decision == "pending")
        status = f"{len(plan.findings)} finding(s), {undecided} undecided"
        if problems:
            print(f"FAIL  {path} ({status})")
            for pr in problems:
                print(f"      {pr}")
            total += len(problems)
        else:
            print(f"ok    {path} ({status})")
        for note in advisories:
            print(f"note  {note}")
    return 1 if total else 0


# --- CLI ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extraction pipeline: raw material -> reviewable plan -> canon + scene.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="run the Extractor over one raw file (LLM)")
    p_plan.add_argument("raw", help="path to a raw/ file")
    p_plan.add_argument("--force", action="store_true", help="overwrite an existing plan")
    p_plan.add_argument("--backend", choices=("cli", "api"), default="cli",
                        help="cli: `claude -p` on your subscription (default); api: metered API")
    p_plan.add_argument("--model", default=EXTRACT_MODEL, help=f"model (default: {EXTRACT_MODEL})")

    p_apply = sub.add_parser("apply", help="materialize a triaged plan (no LLM)")
    p_apply.add_argument("plan", help="path to an extraction/plans/ file")
    p_apply.add_argument("--no-exposition", action="store_true",
                         help="skip emitting the expositions/ file")

    sub.add_parser("check", help="structurally validate pending plans (no LLM)")

    args = ap.parse_args(argv)
    if args.cmd == "plan":
        runner = make_runner(args.backend, model=args.model,
                             max_tokens=EXTRACT_MAX_TOKENS, timeout=EXTRACT_CLI_TIMEOUT)
        return 0 if make_plan(args.raw, runner=runner, force=args.force) else 1
    if args.cmd == "apply":
        return apply_plan(args.plan, scene=not args.no_exposition)
    if args.cmd == "check":
        return check_plans()
    return 1


if __name__ == "__main__":
    sys.exit(main())

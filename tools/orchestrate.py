"""A minimal orchestrator: decompose a goal into a task DAG, run it topologically.

Why this exists
---------------
The role prompts (``agents/``) and the context assembler (``tools.assemble_context``)
cover *one* bounded task at a time. What was missing is the thing that turns a
high-level goal — "develop these three statements' files, then draft and
consistency-check this writeup" — into a set of small, individually-scoped role tasks
and runs them in the right order. That is this module.

Design constraints, each deliberate:

* **The orchestrator handles specs, never prose.** A DAG node is a :class:`Task` =
  ``(role, entity_ids, instruction)`` and nothing else. The orchestrator never loads
  canon bodies into its own context; the model-facing context for a task is assembled
  *at dispatch time* from its entity ids (see :func:`run`), so planning stays cheap no
  matter how large the knowledge base grows.
* **One LLM invocation path.** Dispatch goes through the same cli/api backend the
  capsule generator uses (``tools.capsules.run_llm_{cli,api}``) — there is no second way
  to call a model in this repo. ``cli`` (subscription) is the default; ``api`` is opt-in.
* **Sequential, in-process execution.** The DAG *structure* — fan-out for independent
  entities, pipelines for draft->check->revise chains — is what matters now; real
  concurrency is a later concern, so the scheduler just walks a deterministic
  topological order.
* **Canon safety gate.** After any task whose role edits canon, ``make check`` runs and
  a red gate becomes a *task failure*: the run halts and the remaining tasks are marked
  skipped rather than dispatched on top of broken canon.

Everything the model touches (``runner``, context ``assemble``, role ``load_prompt``,
the canon ``check``) is injectable, so the whole pipeline runs offline under test with
fakes — the same pattern the capsule tests use.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import capsules
from .assemble_context import assemble as _default_assemble
from .common import AGENTS_DIR, ROOT

# Default model for role tasks. Heavier than the capsule tier (summarization -> Haiku);
# proof drafting and consistency reasoning want a stronger general model. Override per
# run via make_runner(model=...). Per-role model tiering can come later — one dial for now.
ORCH_MODEL = "claude-sonnet-5"


# --- roles -------------------------------------------------------------------

@dataclass(frozen=True)
class Role:
    """A knowledge-base role: its system-prompt file and whether it edits canon.

    ``edits_canon`` is what drives the safety gate — the Curator mutates ``canon/``;
    the Drafter, Consistency Editor, and Extractor only propose (raw material, notes,
    plans), so they ride the read-only guard instead.
    """
    name: str
    prompt_file: str
    edits_canon: bool


ROLES: dict[str, Role] = {
    # The Curator (the Lore Keeper's successor): integrates unintegrated blocks,
    # merges duplicates, keeps statuses and the dependency graph honest.
    "curator":            Role("curator", "curator.md", edits_canon=True),
    # The Drafter writes prose — proof attempts, exposition drafts — as raw material
    # under raw/, never directly into canon or expositions (CLAUDE.md rule 9).
    "drafter":            Role("drafter", "drafter.md", edits_canon=False),
    # The Consistency Editor is the notation-drift and restated-theorem police; its
    # output is advisory notes (tools.editor), never edits.
    "consistency-editor": Role("consistency-editor", "consistency-editor.md",
                               edits_canon=False),
    # The Extractor proposes; it never mutates. Its whole output is a plan file under
    # extraction/plans/ (tools.extract), so it rides the same no-edit guard as the
    # other edits_canon=False roles.
    "extractor":          Role("extractor", "extractor.md", edits_canon=False),
}


# --- task spec + DAG ---------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """One node in the DAG: a single role acting on some entities with an instruction.

    Prose-free by construction — ``instruction`` is a terse directive and entities are
    referenced by id. Everything the model needs to *read* is assembled from
    ``entity_ids`` at dispatch time (:func:`run`), never stored here. ``deps`` are the
    ids of tasks that must complete first; ``k`` is the assembler radius for the
    per-task context bundle.
    """
    id: str
    role: str
    entity_ids: tuple[str, ...]
    instruction: str
    deps: tuple[str, ...] = ()
    k: int = 1

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; known: {sorted(ROLES)}")

    @property
    def edits_canon(self) -> bool:
        return ROLES[self.role].edits_canon


@dataclass
class TaskDAG:
    """A set of tasks plus their dependency edges, with a deterministic topo order."""

    tasks: dict[str, Task] = field(default_factory=dict)

    def add(self, task: Task) -> Task:
        if task.id in self.tasks:
            raise ValueError(f"duplicate task id: {task.id!r}")
        self.tasks[task.id] = task
        return task

    def extend(self, tasks: Iterable[Task]) -> "TaskDAG":
        for t in tasks:
            self.add(t)
        return self

    def validate(self) -> "TaskDAG":
        """Raise if any dep points at an unknown task or the graph has a cycle."""
        self.topo_order()
        return self

    def topo_order(self) -> list[str]:
        """Deterministic topological order (Kahn's algorithm, ties broken by task id).

        Deterministic ordering is what makes fan-out reproducible and the tests exact:
        when several tasks are simultaneously ready (i.e. independent), they come out
        sorted by id rather than in dict-insertion order. Raises ``ValueError`` on an
        unknown dependency or a cycle.
        """
        for t in self.tasks.values():
            for d in t.deps:
                if d not in self.tasks:
                    raise ValueError(f"task {t.id!r} depends on unknown task {d!r}")

        indeg = {tid: 0 for tid in self.tasks}
        dependents: dict[str, list[str]] = {tid: [] for tid in self.tasks}
        for t in self.tasks.values():
            for d in t.deps:
                indeg[t.id] += 1
                dependents[d].append(t.id)

        ready = sorted(tid for tid, n in indeg.items() if n == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            newly_ready = False
            for dep in dependents[tid]:
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    ready.append(dep)
                    newly_ready = True
            if newly_ready:
                ready.sort()

        if len(order) != len(self.tasks):
            cyc = sorted(set(self.tasks) - set(order))
            raise ValueError(f"cycle detected among tasks: {cyc}")
        return order


# --- DAG builders: the two archetypal shapes ---------------------------------

def fan_out(role: str, entity_ids: Sequence[str], instruction: str, *,
            prefix: str | None = None, k: int = 1) -> list[Task]:
    """One independent task per entity — the fan-out pattern.

    None of the returned tasks depends on another, so the scheduler is free to run them
    in any order (we run them sorted, deterministically). Use when the same operation
    applies to several entities that do not touch each other, e.g. integrate three
    unrelated entities' unintegrated blocks.
    """
    prefix = prefix or role
    return [
        Task(id=f"{prefix}:{eid}", role=role, entity_ids=(eid,),
             instruction=instruction, k=k)
        for eid in entity_ids
    ]


def pipeline(entity_ids: Sequence[str], stages: Sequence[tuple[str, str]], *,
             base_id: str, k: int = 1) -> list[Task]:
    """A linear chain of role stages over the same entities — the pipeline pattern.

    ``stages`` is an ordered list of ``(role, instruction)``; stage *i* depends on stage
    *i-1*, so at dispatch time each stage receives the previous stage's output (see
    :func:`build_task_prompt`). This is how a draft -> consistency-check -> revise chain
    is expressed.
    """
    tasks: list[Task] = []
    prev: str | None = None
    for i, (role, instruction) in enumerate(stages):
        tid = f"{base_id}:{i}:{role}"
        tasks.append(Task(
            id=tid, role=role, entity_ids=tuple(entity_ids),
            instruction=instruction, deps=(prev,) if prev else (), k=k,
        ))
        prev = tid
    return tasks


# --- goal decomposition ------------------------------------------------------

@dataclass(frozen=True)
class WriteupGoal:
    """A writeup to realise: which entities it involves and a terse (prose-free) outline."""
    slug: str
    entity_ids: tuple[str, ...]
    outline: str


@dataclass(frozen=True)
class Goal:
    """A structured, prose-free description of a unit of knowledge-base work.

    Structured on purpose: decomposition into a DAG is *deterministic*, so the
    orchestrator plans without an LLM and without holding any canon prose. Each field
    maps to one of the two archetypal shapes — fan-out (``integrate``) and pipeline
    (``writeups``) — plus an optional single Curator reconciliation sweep.
    """
    summary: str = ""
    integrate: tuple[str, ...] = ()        # entity ids whose unintegrated blocks to compact
    integrate_instruction: str = ("Integrate this entity's unintegrated extraction "
                                  "blocks into settled prose, then delete the blocks.")
    writeups: tuple[WriteupGoal, ...] = () # each -> a draft/check/revise pipeline
    reconcile: tuple[str, ...] = ()        # ids for a single Curator consistency sweep


# The canonical draft -> consistency-check -> revise chain. The middle stage flags
# issues; the last stage (Drafter again) revises the prose to resolve them. Roles only,
# no prose.
WRITEUP_STAGES: tuple[tuple[str, str], ...] = (
    ("drafter", "Draft this writeup from canon and the outline."),
    ("consistency-editor", "Check the draft against canon — statements quoted "
                           "faithfully, notation per the index, no reproved or "
                           "contradicted results; list any issues."),
    ("drafter", "Revise the draft to resolve the consistency notes."),
)


def decompose(goal: Goal) -> TaskDAG:
    """Turn a high-level :class:`Goal` into a validated task DAG.

    Independent integration work fans out; each writeup becomes its own
    draft/check/revise pipeline; an optional reconcile sweep is one Curator task over
    many ids. The result is validated (deps resolve, no cycles) before it is returned.
    """
    dag = TaskDAG()
    if goal.integrate:
        dag.extend(fan_out("curator", goal.integrate,
                           goal.integrate_instruction, prefix="integrate"))
    if goal.reconcile:
        dag.add(Task(
            id="reconcile", role="curator", entity_ids=tuple(goal.reconcile),
            instruction="Reconcile these entities against canon and the dependency graph.",
        ))
    for writeup in goal.writeups:
        stages = list(WRITEUP_STAGES)
        # thread the writeup's own (terse) outline into the drafting stage's instruction
        stages[0] = (stages[0][0], f"{stages[0][1]} Outline: {writeup.outline}")
        dag.extend(pipeline(writeup.entity_ids, stages, base_id=f"writeup:{writeup.slug}"))
    return dag.validate()


# --- runner: the single, injectable LLM path ---------------------------------

Runner = Callable[[str, str], str]
"""A model backend: ``(system_prompt, user_prompt) -> completion text``."""


def make_runner(backend: str = "cli", *, model: str = ORCH_MODEL, client=None,
                run=None, binary: str | None = None,
                max_tokens: int | None = None, timeout: int | None = None) -> Runner:
    """Build a :data:`Runner` bound to one backend.

    ``cli`` (Claude subscription) is the default; ``api`` (metered Messages API) is
    opt-in. Both delegate to :mod:`tools.capsules`, so there is a single LLM invocation
    path in the repo. Tests inject their own Runner rather than calling this.

    ``max_tokens`` (api) and ``timeout`` (cli) default to the capsule-sized budgets in
    :mod:`tools.capsules`; callers producing long completions (e.g. ``tools.extract``'s
    whole-document plans) raise them here rather than growing a second backend.
    """
    if backend == "cli":
        _run = run or subprocess.run
        cli_kwargs = {"timeout": timeout} if timeout is not None else {}
        return lambda system, prompt: capsules.run_llm_cli(
            system, prompt, model=model, binary=binary, run=_run, **cli_kwargs)
    if backend == "api":
        if client is None:
            import anthropic  # lazy: not needed for the cli backend or tests
            client = anthropic.Anthropic()
        api_kwargs = {"max_tokens": max_tokens} if max_tokens is not None else {}
        return lambda system, prompt: capsules.run_llm_api(
            client, system, prompt, model=model, **api_kwargs)
    raise ValueError(f"unknown backend {backend!r} (expected 'cli' or 'api')")


def load_role_prompt(role: str, *, agents_dir: Path = AGENTS_DIR) -> str:
    """Read a role's system prompt from ``agents/<role>.md``."""
    return (Path(agents_dir) / ROLES[role].prompt_file).read_text()


def build_task_prompt(task: Task, context: str, upstream: dict[str, str]) -> str:
    """Assemble the user-facing prompt for one task, at dispatch time.

    The orchestrator's own context stays prose-free; the prose lives *here*, built fresh
    per dispatch from (a) the assembler's bounded bundle and (b) upstream stage outputs
    (the pipeline hand-off). Nothing is retained on the DAG.
    """
    parts = [
        f"# Task\n{task.instruction}",
        f"# Entities in scope\n{', '.join(task.entity_ids)}",
    ]
    if upstream:
        joined = "\n\n".join(f"## Output of {tid}\n{text}" for tid, text in upstream.items())
        parts.append(f"# Upstream results\n{joined}")
    parts.append(f"# Assembled context\n{context}")
    return "\n\n".join(parts)


def run_make_check(*, cwd: Path = ROOT, run=None) -> tuple[bool, str]:
    """Run ``make check`` and return ``(ok, combined_output)`` — the canon safety gate."""
    _run = run or subprocess.run
    result = _run(["make", "check"], cwd=str(cwd), capture_output=True, text=True)
    ok = result.returncode == 0
    return ok, ((result.stdout or "") + (result.stderr or "")).strip()


# Paths a read-only role (Drafter, Consistency Editor, Extractor) must never touch.
# canon/ is read-only truth to these roles, and expositions/ is written only by the
# extraction pipeline's deterministic apply step — a Drafter's output is raw material
# under raw/, reaching expositions/ via human triage (CLAUDE.md rule 9,
# agents/drafter.md). Both trees are watched, not just canon/, because a role placing
# or rewriting a writeup directly is exactly the kind of out-of-scope edit this guard
# exists to catch.
NO_EDIT_WATCH_PATHS: tuple[str, ...] = ("canon/", "expositions/")

# Where the guard quarantines the content it is about to undo (see run_no_edit_guard).
# Lives at the repo root, deliberately *outside* the watched paths so the salvage copy
# itself can never re-trip the guard; gitignored so it never dirties a commit.
SALVAGE_DIR_NAME = ".orchestrate-salvage"


def _salvage_dirty_paths(cwd: Path, dirty: str) -> Path | None:
    """Copy everything a dirty ``git status --porcelain`` names into a quarantine dir.

    The guard's restore step is destructive by design — ``git checkout --`` discards
    tracked modifications and ``git clean -fd`` deletes untracked files outright. That
    is the correct *tree* outcome (a read-only role's edits must not survive), but the
    bytes themselves may be hours of irreplaceable LLM or human prose: the ancestor
    spine lost a fully drafted writeup to exactly this code path, when a test invoked
    the guard against the real working tree instead of a temp repo. So before the
    restore runs, every reported path still on disk is copied (tree structure
    preserved) into ``<repo>/.orchestrate-salvage/<timestamp>/``, turning "deleted"
    into "quarantined, recoverable by hand".

    Best-effort on purpose: a line that can't be parsed or a file that can't be copied
    is skipped rather than raised, because aborting here would leave the *violation* in
    place — salvage must never block the restore it exists to soften. Returns the
    salvage dir, or None if nothing was copyable (e.g. every entry was a deletion).
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_root = cwd / SALVAGE_DIR_NAME / stamp
    n = 0
    while dest_root.exists():  # same-second collision: suffix rather than merge runs
        n += 1
        dest_root = cwd / SALVAGE_DIR_NAME / f"{stamp}-{n}"

    copied = False
    for line in dirty.splitlines():
        if len(line) < 4 or not line.strip():
            continue
        raw = line[3:]  # porcelain v1: two status chars + space, then the path
        if " -> " in raw:  # rename: the right-hand side is where the content lives now
            raw = raw.split(" -> ", 1)[1]
        rel = raw.strip().strip('"')  # quoted-path handling is best-effort by design
        src = cwd / rel
        try:
            if src.is_file():
                dest = dest_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied = True
            elif src.is_dir():  # an entire untracked directory (git reports it as one line)
                shutil.copytree(src, dest_root / rel, dirs_exist_ok=True)
                copied = True
        except OSError:
            continue
    return dest_root if copied else None


def run_no_edit_guard(*, cwd: Path = ROOT, run=None,
                      paths: tuple[str, ...] = NO_EDIT_WATCH_PATHS) -> tuple[bool, str]:
    """The inverse of :func:`run_make_check`: a read-only role must leave no git trace.

    Where the canon safety gate asks "did this canon-editing task leave canon valid?",
    this asks "did this *read-only* task (``edits_canon=False``) touch
    canon/expositions at all?" It exists because ``runner`` may, in the ``cli``
    backend, be a real ``claude -p`` subprocess that — depending on the caller's tool
    permissions — could use file-editing tools despite its role prompt saying not to; a
    prose instruction is not a mechanical guarantee, so this checks the working tree
    instead of trusting the prompt.

    A non-empty scoped ``git status --porcelain`` (tracked modifications/deletions *and*
    untracked new files — a stray new file is exactly as much a violation as an edited
    one) means the role wrote where it should not have. The violation is undone two ways:
    ``git checkout --`` restores every tracked path git status reported, and ``git clean
    -fd --`` removes any untracked file/directory it left behind — together, a full
    restore of the watched paths, not just the tracked-file half of it. Both restore
    commands are given only the watched paths that actually exist on disk: unlike
    ``git status``, ``git checkout --`` and ``git clean --`` *error out entirely* on a
    pathspec that matches nothing, which would otherwise abort the restore of the
    *other*, real violation alongside it. ``run`` is injected so tests exercise real
    git against a disposable temp repo rather than faking subprocess output (git's own
    status/checkout semantics are the thing under test).

    The restore never *destroys* content: everything it is about to discard is first
    copied into a gitignored quarantine (see :func:`_salvage_dirty_paths` for the
    incident that motivated this), and the returned detail names the salvage dir so a
    human can recover legitimate work the guard caught by mistake.
    """
    _run = run or subprocess.run
    status = _run(["git", "status", "--porcelain", "--", *paths],
                  cwd=str(cwd), capture_output=True, text=True)
    dirty = (status.stdout or "").strip()
    if not dirty:
        return True, ""

    # Parse the raw (unstripped) stdout: porcelain's two status chars can start with a
    # space (" M "), which the display-oriented strip() above would eat off line one.
    salvage = _salvage_dirty_paths(Path(cwd), status.stdout or "")
    present = [p for p in paths if (Path(cwd) / p.rstrip("/")).exists()]
    if present:
        _run(["git", "checkout", "--", *present], cwd=str(cwd), capture_output=True, text=True)
        _run(["git", "clean", "-fd", "--", *present], cwd=str(cwd), capture_output=True, text=True)
    where = f"; discarded content salvaged to {salvage}" if salvage else ""
    return False, f"read-only role modified {', '.join(paths)} (restored{where}):\n{dirty}"


# --- execution ---------------------------------------------------------------

@dataclass
class TaskOutcome:
    task_id: str
    status: str            # "ok" | "failed" | "skipped"
    output: str = ""
    error: str = ""


@dataclass
class RunReport:
    outcomes: list[TaskOutcome] = field(default_factory=list)

    def ok(self) -> bool:
        return all(o.status == "ok" for o in self.outcomes)

    @property
    def outputs(self) -> dict[str, str]:
        return {o.task_id: o.output for o in self.outcomes if o.status == "ok"}

    def failed(self) -> TaskOutcome | None:
        return next((o for o in self.outcomes if o.status == "failed"), None)


def _assemble_all(task: Task, assemble: Callable[..., str]) -> str:
    """Concatenate the per-entity context bundles for a multi-entity task."""
    return "\n\n".join(
        assemble(eid, k=task.k, full_neighbors=False, budget=None)
        for eid in task.entity_ids
    )


def run(
    dag: TaskDAG,
    runner: Runner,
    *,
    assemble: Callable[..., str] = _default_assemble,
    load_prompt: Callable[[str], str] = load_role_prompt,
    check: Callable[[], tuple[bool, str]] = run_make_check,
    enforce_no_edits: Callable[[], tuple[bool, str]] = run_no_edit_guard,
) -> RunReport:
    """Execute the DAG in a deterministic topological order, sequentially, in-process.

    For each task: assemble its context from its entity ids, gather its dependencies'
    outputs, build the prompt, and dispatch through ``runner``. Afterward, exactly one
    of two safety gates runs, chosen by the role's ``edits_canon`` flag: a canon-editing
    task runs ``check`` (``make check``; a red gate is a task failure); a read-only task
    instead runs ``enforce_no_edits`` — the inverse check (:func:`run_no_edit_guard`)
    that the task left ``canon/``/``expositions/`` untouched. Either gate failing halts
    the run — remaining tasks are marked skipped rather than dispatched on top of broken
    canon or an unenforced read-only violation.

    ``runner``, ``assemble``, ``load_prompt``, ``check`` and ``enforce_no_edits`` are
    all injected so this runs fully offline under test.
    """
    results: dict[str, str] = {}
    report = RunReport()
    halted = False

    for tid in dag.topo_order():
        task = dag.tasks[tid]
        if halted:
            report.outcomes.append(TaskOutcome(tid, "skipped"))
            continue

        context = _assemble_all(task, assemble)
        upstream = {d: results[d] for d in task.deps}
        prompt = build_task_prompt(task, context, upstream)
        system = load_prompt(task.role)

        try:
            output = runner(system, prompt)
        except Exception as exc:  # a backend error is this task's failure, not a crash
            report.outcomes.append(TaskOutcome(tid, "failed", error=f"runner error: {exc}"))
            halted = True
            continue

        results[tid] = output

        if task.edits_canon:
            gate_ok, detail = check()
            if not gate_ok:
                report.outcomes.append(TaskOutcome(
                    tid, "failed", output=output,
                    error=f"make check failed after {tid}:\n{detail}"))
                halted = True
                continue
        else:
            guard_ok, detail = enforce_no_edits()
            if not guard_ok:
                report.outcomes.append(TaskOutcome(
                    tid, "failed", output=output,
                    error=f"read-only guard tripped after {tid}:\n{detail}"))
                halted = True
                continue

        report.outcomes.append(TaskOutcome(tid, "ok", output=output))

    return report


# --- CLI ---------------------------------------------------------------------

def format_plan(dag: TaskDAG) -> str:
    """Human-readable dump of the plan (topo order + specs), for --dry-run."""
    lines = []
    for tid in dag.topo_order():
        t = dag.tasks[tid]
        deps = f"  <- {', '.join(t.deps)}" if t.deps else ""
        gate = " [canon-gate]" if t.edits_canon else ""
        lines.append(f"{tid}  ({t.role}, entities={','.join(t.entity_ids)}){gate}{deps}")
        lines.append(f"    {t.instruction}")
    return "\n".join(lines)


def _load_goal(path: Path) -> Goal:
    """Load a Goal from a JSON file. Writeups are ``{slug, entity_ids, outline}`` objects."""
    import json

    data = json.loads(Path(path).read_text())
    writeups = tuple(
        WriteupGoal(s["slug"], tuple(s["entity_ids"]), s["outline"])
        for s in data.get("writeups", [])
    )
    return Goal(
        summary=data.get("summary", ""),
        integrate=tuple(data.get("integrate", [])),
        integrate_instruction=data.get("integrate_instruction", Goal.integrate_instruction),
        writeups=writeups,
        reconcile=tuple(data.get("reconcile", [])),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decompose a goal into a task DAG and run it.")
    ap.add_argument("goal", help="path to a JSON goal file (see _load_goal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan (topo order + specs) and exit; no LLM calls")
    ap.add_argument("--backend", choices=("cli", "api"), default="cli",
                    help="cli: `claude -p` on your subscription (default); api: metered API")
    ap.add_argument("--model", default=ORCH_MODEL, help=f"model (default: {ORCH_MODEL})")
    args = ap.parse_args(argv)

    dag = decompose(_load_goal(Path(args.goal)))
    if args.dry_run:
        print(format_plan(dag))
        return 0

    report = run(dag, make_runner(args.backend, model=args.model))
    for o in report.outcomes:
        mark = {"ok": "OK  ", "failed": "FAIL", "skipped": "SKIP"}[o.status]
        print(f"{mark} {o.task_id}")
        if o.error:
            print(f"     {o.error.splitlines()[0]}")
    if not report.ok():
        f = report.failed()
        print(f"\nFAILED at {f.task_id}:\n{f.error}" if f else "\nFAILED.")
        return 1
    print(f"\nOK — {len(report.outcomes)} task(s) completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

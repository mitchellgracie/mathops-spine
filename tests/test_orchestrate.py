"""Tests for the task-DAG orchestrator (tools.orchestrate).

All offline: the LLM, the context assembler, the role-prompt loader, and the `make
check` gate are injected as fakes (same pattern as test_capsules' RecordingClient /
FakeRun), so nothing shells out, hits the network, or touches real canon. What we pin is
the structure that matters — DAG construction, deterministic topological order, fan-out
independence, pipeline data hand-off — and the canon safety gate's halt-on-failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import orchestrate as orch


# --- fakes -------------------------------------------------------------------

class RecordingRunner:
    """Stand-in for a model backend: records (system, prompt) per call, returns canned text.

    `reply` may be a str or a callable(system, prompt, call_index) -> str so a test can
    make outputs distinguishable per stage.
    """

    def __init__(self, reply="OUTPUT"):
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, prompt: str) -> str:
        text = self._reply(system, prompt, len(self.calls)) if callable(self._reply) else self._reply
        self.calls.append((system, prompt))
        return text


def fake_assemble(eid, *, k=1, full_neighbors=False, budget=None):
    """Deterministic stand-in for assemble_context.assemble: a marker per entity id."""
    return f"CTX[{eid} k={k}]"


def fake_prompt(role: str) -> str:
    """Deterministic stand-in for load_role_prompt."""
    return f"SYSTEM<{role}>"


def run(dag, runner, **kw):
    """orchestrate.run with the offline fakes wired in by default.

    ``enforce_no_edits`` defaults to a no-op fake too: without it, every read-only
    (``edits_canon=False``) task in these tests would fall through to the real
    :func:`orch.run_no_edit_guard`, which shells out to real git against this repo's
    actual working tree — exactly the network/filesystem side effect this module's
    docstring promises never happens here. The guard itself is exercised against a real
    (disposable, temp-dir) git repo in ``tests/test_editor.py``.
    """
    kw.setdefault("assemble", fake_assemble)
    kw.setdefault("load_prompt", fake_prompt)
    kw.setdefault("check", lambda: (True, ""))
    kw.setdefault("enforce_no_edits", lambda: (True, ""))
    return orch.run(dag, runner, **kw)


# --- DAG construction + topological order ------------------------------------

def test_fan_out_tasks_are_independent():
    tasks = orch.fan_out("curator", ["thm.a", "thm.b", "thm.c"], "integrate it")
    dag = orch.TaskDAG().extend(tasks).validate()

    assert len(dag.tasks) == 3
    # each task owns exactly one entity and has no dependencies -> nothing serialises them
    for t in dag.tasks.values():
        assert len(t.entity_ids) == 1
        assert t.deps == ()
        assert t.edits_canon  # the curator mutates canon
    # id prefix defaults to the role name; all three are simultaneously ready, so the
    # deterministic order is simply sorted by id
    assert dag.topo_order() == ["curator:thm.a", "curator:thm.b", "curator:thm.c"]


def test_pipeline_is_a_linear_chain():
    stages = [("drafter", "draft"), ("consistency-editor", "check"), ("drafter", "revise")]
    tasks = orch.pipeline(["thm.y"], stages, base_id="writeup:w1")
    dag = orch.TaskDAG().extend(tasks).validate()

    order = dag.topo_order()
    assert order == ["writeup:w1:0:drafter", "writeup:w1:1:consistency-editor",
                     "writeup:w1:2:drafter"]
    # each stage depends on exactly its predecessor
    assert dag.tasks[order[0]].deps == ()
    assert dag.tasks[order[1]].deps == (order[0],)
    assert dag.tasks[order[2]].deps == (order[1],)


def test_topo_order_respects_deps_across_fanout_and_pipeline():
    # two independent fan-out tasks + a pipeline; a pipeline stage depends on a fan-out task
    dag = orch.TaskDAG()
    dag.extend(orch.fan_out("curator", ["thm.a", "thm.b"], "integrate",
                            prefix="integrate"))
    p = orch.pipeline(["thm.x"], [("drafter", "draft"), ("consistency-editor", "check")],
                      base_id="writeup:w")
    dag.extend(p)
    # make the draft wait on thm.a's integration
    dag.tasks["writeup:w:0:drafter"] = orch.Task(
        id="writeup:w:0:drafter", role="drafter", entity_ids=("thm.x",),
        instruction="draft", deps=("integrate:thm.a",))
    dag.validate()

    order = dag.topo_order()
    assert order.index("integrate:thm.a") < order.index("writeup:w:0:drafter")
    assert order.index("writeup:w:0:drafter") < order.index("writeup:w:1:consistency-editor")


def test_cycle_is_rejected():
    dag = orch.TaskDAG()
    dag.add(orch.Task("a", "drafter", ("thm.x",), "i", deps=("b",)))
    dag.add(orch.Task("b", "drafter", ("thm.x",), "i", deps=("a",)))
    try:
        dag.topo_order()
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("expected a cycle to be rejected")


def test_unknown_dep_is_rejected():
    dag = orch.TaskDAG()
    dag.add(orch.Task("a", "drafter", ("thm.x",), "i", deps=("ghost",)))
    try:
        dag.validate()
    except ValueError as exc:
        assert "ghost" in str(exc)
    else:
        raise AssertionError("expected an unknown dependency to be rejected")


def test_unknown_role_is_rejected():
    try:
        orch.Task("a", "nonsense", ("thm.x",), "i")
    except ValueError as exc:
        assert "unknown role" in str(exc)
    else:
        raise AssertionError("expected an unknown role to be rejected")


def test_decompose_builds_fanout_plus_pipelines():
    goal = orch.Goal(
        summary="integrate + a writeup",
        integrate=("thm.a", "thm.b"),
        writeups=(orch.WriteupGoal("w1", ("thm.x",), "survey the bound and its variants"),),
        reconcile=("def.d1",),
    )
    dag = orch.decompose(goal)  # validates internally

    # fan-out: two independent curator tasks
    assert dag.tasks["integrate:thm.a"].deps == ()
    assert dag.tasks["integrate:thm.b"].deps == ()
    # one curator sweep over all reconcile ids
    assert dag.tasks["reconcile"].entity_ids == ("def.d1",)
    assert dag.tasks["reconcile"].role == "curator"
    # writeup pipeline: draft -> consistency-check -> revise, a linear chain
    chain = ["writeup:w1:0:drafter", "writeup:w1:1:consistency-editor",
             "writeup:w1:2:drafter"]
    assert all(c in dag.tasks for c in chain)
    assert dag.tasks[chain[1]].deps == (chain[0],)
    assert dag.tasks[chain[2]].deps == (chain[1],)
    # the terse outline is threaded into the drafting instruction, not stored as prose elsewhere
    assert "survey the bound and its variants" in dag.tasks[chain[0]].instruction


# --- execution: dispatch, context, fan-out independence ----------------------

def test_run_dispatches_each_task_with_assembled_context_and_role_prompt():
    dag = orch.TaskDAG().extend(
        orch.fan_out("drafter", ["thm.a", "thm.b"], "draft it")).validate()
    runner = RecordingRunner("done")

    report = run(dag, runner)

    assert report.ok()
    assert len(runner.calls) == 2
    # order follows the deterministic topo order (sorted ids)
    (sys_a, prompt_a), (sys_b, prompt_b) = runner.calls
    assert sys_a == fake_prompt("drafter")            # role system prompt reached the model
    assert "CTX[thm.a k=1]" in prompt_a               # context assembled from THIS task's entity
    assert "thm.a" in prompt_a and "thm.b" not in prompt_a  # fan-out tasks are isolated
    assert "CTX[thm.b k=1]" in prompt_b and "thm.a" not in prompt_b
    assert report.outputs == {"drafter:thm.a": "done", "drafter:thm.b": "done"}


def test_pipeline_passes_upstream_output_downstream():
    stages = [("drafter", "draft"), ("consistency-editor", "check"), ("drafter", "revise")]
    dag = orch.TaskDAG().extend(
        orch.pipeline(["thm.x"], stages, base_id="writeup:w")).validate()
    # each stage returns a distinct, identifiable output
    runner = RecordingRunner(lambda system, prompt, i: f"OUT{i}")

    report = run(dag, runner)
    assert report.ok()

    (_, p0), (_, p1), (_, p2) = runner.calls
    # stage 0 has no upstream; stages 1 and 2 receive their predecessor's output
    assert "Upstream results" not in p0
    assert "OUT0" in p1 and "Output of writeup:w:0:drafter" in p1
    assert "OUT1" in p2 and "Output of writeup:w:1:consistency-editor" in p2
    # and it never leaks two hops: stage 2 sees stage 1's output, not stage 0's raw text
    assert "OUT0" not in p2


# --- canon safety gate -------------------------------------------------------

def test_canon_editing_task_runs_check_and_halts_run_on_failure():
    # a curator task (edits canon) that a drafter task depends on
    dag = orch.TaskDAG()
    dag.add(orch.Task("integrate", "curator", ("thm.a",), "integrate"))
    dag.add(orch.Task("draft", "drafter", ("thm.x",), "draft", deps=("integrate",)))
    dag.validate()

    checks = []

    def failing_check():
        checks.append(True)
        return False, "schema validation failed"

    runner = RecordingRunner("done")
    report = run(dag, runner, check=failing_check)

    assert not report.ok()
    assert len(checks) == 1                          # gate ran once, for the canon task
    outcomes = {o.task_id: o.status for o in report.outcomes}
    assert outcomes == {"integrate": "failed", "draft": "skipped"}  # downstream skipped
    assert len(runner.calls) == 1                    # the drafter was never invoked
    assert "schema validation failed" in report.failed().error


def test_gate_runs_only_for_canon_tasks_and_passes():
    dag = orch.TaskDAG()
    dag.add(orch.Task("integrate", "curator", ("thm.a",), "integrate"))
    dag.add(orch.Task("draft", "drafter", ("thm.x",), "draft", deps=("integrate",)))
    dag.validate()

    checks = []
    report = run(dag, RecordingRunner("ok"), check=lambda: (checks.append(True) or (True, "")))

    assert report.ok()
    assert len(checks) == 1  # only the curator task triggers the gate


def test_read_only_guard_failure_halts_run():
    dag = orch.TaskDAG()
    dag.add(orch.Task("draft", "drafter", ("thm.x",), "draft"))
    dag.add(orch.Task("check", "consistency-editor", ("thm.x",), "check", deps=("draft",)))
    dag.validate()

    report = run(dag, RecordingRunner("out"),
                 enforce_no_edits=lambda: (False, "drafter wrote into canon/"))
    assert not report.ok()
    outcomes = {o.task_id: o.status for o in report.outcomes}
    assert outcomes == {"draft": "failed", "check": "skipped"}
    assert "wrote into canon/" in report.failed().error


def test_runner_error_becomes_task_failure_and_halts():
    dag = orch.TaskDAG()
    dag.add(orch.Task("a", "drafter", ("thm.x",), "draft"))
    dag.add(orch.Task("b", "drafter", ("thm.y",), "draft", deps=("a",)))
    dag.validate()

    def boom(system, prompt):
        raise RuntimeError("backend exploded")

    report = run(dag, boom)
    assert not report.ok()
    outcomes = {o.task_id: o.status for o in report.outcomes}
    assert outcomes == {"a": "failed", "b": "skipped"}
    assert "backend exploded" in report.failed().error


# --- the single LLM invocation path ------------------------------------------

class FakeRun:
    """Stand-in for subprocess.run: records argv, returns canned stdout (as in test_capsules)."""

    def __init__(self, stdout="ROLE OUTPUT", returncode=0, stderr=""):
        from types import SimpleNamespace
        self.result = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
        self.cmd = None

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        return self.result


def test_make_runner_cli_routes_through_capsules_single_path():
    fake = FakeRun("drafted writeup\n")
    runner = orch.make_runner("cli", model="claude-sonnet-5", run=fake, binary="claude")

    text = runner("SYSTEM<drafter>", "do the thing")

    assert text == "drafted writeup"                  # stdout, stripped
    # the command is built by tools.capsules.run_llm_cli — one invocation path
    cmd = fake.cmd
    assert cmd[0] == "claude" and "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM<drafter>"
    assert cmd[-1] == "do the thing"


def test_make_runner_rejects_unknown_backend():
    try:
        orch.make_runner("telepathy")
    except ValueError as exc:
        assert "unknown backend" in str(exc)
    else:
        raise AssertionError("expected an unknown backend to be rejected")


def test_role_registry_gates_are_correctly_assigned():
    # The curator is the one canon-editing role; the proposal-only roles ride the
    # read-only guard, and every registered role has a non-empty prompt file.
    assert orch.ROLES["curator"].edits_canon
    for name in ("drafter", "consistency-editor", "extractor"):
        assert not orch.ROLES[name].edits_canon
    for name in orch.ROLES:
        assert orch.load_role_prompt(name).strip()  # prompt file exists and is non-empty


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

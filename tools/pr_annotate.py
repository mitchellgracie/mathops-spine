"""PR-scoped annotation report: diff -> touched entity ids -> filtered validator findings.

Given a base ref (default `origin/main`), this reports what a PR actually touches inside
`canon/` and only the validator findings relevant to that diff, so a reviewer (human or
the LLM canon-check) doesn't have to wade through the whole-canon `validate --json`
output to see whether a change introduced or worsened anything. It is a *filter* over the existing gate, not a second rule engine: the
validator still runs whole-canon underneath (cross-entity checks like relation reciprocity
and timeline ordering genuinely need the full graph to reason about); this module only
narrows what gets *displayed* to the ids and files the diff actually touched.

Run as: python -m tools.pr_annotate [--base <ref>]

Caveat inherited from `git diff <ref>` semantics: this only sees *tracked* changes (an
uncommitted, unstaged new file under `canon/` is invisible to `git diff`, same as any other
git-diff-based tool). In CI this is a non-issue — the PR branch's changes are committed by
the time a workflow runs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from .common import ROOT
from .loader import Canon, load_canon
from .validate import Finding, validate


class BaseRefError(RuntimeError):
    """The base ref could not be diffed against (typo, shallow clone, missing remote)."""


@dataclass
class DiffEntry:
    status: str                 # git's raw status field: "A", "M", "D", "R100", ...
    path: str                   # repo-relative path (rename/copy: the new path)
    old_path: str | None = None  # set for renames/copies


@dataclass
class DiffSummary:
    new_ids: list[str] = field(default_factory=list)
    changed_ids: list[str] = field(default_factory=list)
    removed: list[tuple[str, str | None]] = field(default_factory=list)   # (path, id|None)
    unresolved: list[str] = field(default_factory=list)                   # touched, no id found
    touched_files: set[str] = field(default_factory=set)   # repo-relative, for finding-filter


def git_diff_name_status(base: str, *, root: Path = ROOT) -> list[DiffEntry]:
    """Parse `git diff --name-status <base> -- canon/`, run from `root`.

    Raises BaseRefError with git's own stderr on a bad ref, rather than letting a
    CalledProcessError with an opaque traceback surface to the CLI.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-status", base, "--", "canon/"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "unknown error"
        raise BaseRefError(f"git diff against base ref {base!r} failed: {stderr}")

    entries: list[DiffEntry] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        if status[0] in ("R", "C") and len(fields) == 3:
            entries.append(DiffEntry(status=status, path=fields[2], old_path=fields[1]))
        else:
            entries.append(DiffEntry(status=status, path=fields[1]))
    return entries


def _entity_id_at(path: str, ref: str | None, *, root: Path = ROOT) -> str | None:
    """Best-effort `id` frontmatter field of `path` at `ref` (None = current working tree).

    Used for deleted files, whose content no longer exists on disk to run through the
    normal directory loader — but a git-historical read of the base ref still has it, which
    is enough to report *what* was removed without needing the full schema to validate
    (the entity is gone; there is nothing left to validate).
    """
    if ref is None:
        try:
            text = (root / path).read_text()
        except OSError:
            return None
    else:
        proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root,
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return None
        text = proc.stdout
    try:
        post = frontmatter.loads(text)
    except Exception:
        return None
    entity_id = post.metadata.get("id")
    return entity_id if isinstance(entity_id, str) else None


def summarize_diff(base: str, *, root: Path = ROOT, canon: Canon | None = None) -> DiffSummary:
    """Classify every `canon/`-scoped diff entry against `base` as new / changed / removed.

    `canon` is injectable (already-loaded current canon) so callers that also need the full
    canon for validation don't pay to load it twice; None loads `root/canon` fresh.
    """
    entries = git_diff_name_status(base, root=root)
    canon = load_canon(root / "canon") if canon is None else canon
    path_to_id = {str(p.relative_to(root)): eid for eid, p in canon.paths.items()}

    summary = DiffSummary()
    removed_ids: dict[str, str] = {}   # id -> path, for the rename-as-delete+add reconciliation
    for e in entries:
        summary.touched_files.add(e.path)
        if e.status.startswith("D"):
            eid = _entity_id_at(e.path, base, root=root)
            summary.removed.append((e.path, eid))
            if eid is not None:
                removed_ids[eid] = e.path
            continue

        eid = path_to_id.get(e.path) or _entity_id_at(e.path, None, root=root)
        if eid is None:
            summary.unresolved.append(e.path)
            continue
        if e.status.startswith("A") or e.status.startswith("C"):
            summary.new_ids.append(eid)
        else:  # M, R, T, ...
            summary.changed_ids.append(eid)

    # Reconcile the case where rename detection is off (the git default for `diff`) and a
    # renamed file shows up as a D + A pair: same id in both `removed` and `new_ids` means
    # the entity moved, not one entity vanishing while an unrelated one appeared — treat it
    # as changed, matching what `-M` rename detection would have reported directly.
    moved = set(summary.new_ids) & set(removed_ids)
    if moved:
        summary.new_ids = [i for i in summary.new_ids if i not in moved]
        summary.changed_ids.extend(sorted(moved))
        summary.removed = [(p, i) for p, i in summary.removed if i not in moved]

    summary.new_ids = sorted(set(summary.new_ids))
    summary.changed_ids = sorted(set(summary.changed_ids) - set(summary.new_ids))
    return summary


def filter_findings(findings: list[Finding], ids: set[str], files: set[str]) -> list[Finding]:
    """Keep only findings that name a touched entity id or a touched file.

    Cross-entity checks (relation reciprocity, timeline order, ...) attribute a finding to
    one "side" via `id`/`field` even though two entities are involved — see
    `tools.validate._report_contradiction`. That means a finding surfaces here whenever
    *either* the PR's own diff mentions the attributed id/file, which is the right scope: a
    change to entity B that newly contradicts untouched entity A should still show up when
    reviewing B's diff, attributed to B.
    """
    return [f for f in findings
            if (f.id is not None and f.id in ids) or (f.file is not None and f.file in files)]


def render_report(base: str, summary: DiffSummary, findings: list[Finding]) -> str:
    lines = [f"## PR annotation (base: `{base}`)", ""]

    if not (summary.new_ids or summary.changed_ids or summary.removed or summary.unresolved):
        lines.append("No canon changes detected against this base.")
        return "\n".join(lines) + "\n"

    if summary.new_ids:
        lines.append(f"**New entities ({len(summary.new_ids)}):**")
        lines.extend(f"- `{i}`" for i in summary.new_ids)
        lines.append("")
    if summary.changed_ids:
        lines.append(f"**Changed entities ({len(summary.changed_ids)}):**")
        lines.extend(f"- `{i}`" for i in summary.changed_ids)
        lines.append("")
    if summary.removed:
        lines.append(f"**Removed files ({len(summary.removed)}):**")
        lines.extend(f"- `{p}`" + (f" (was `{i}`)" if i else " (id unknown)")
                     for p, i in summary.removed)
        lines.append("")
    if summary.unresolved:
        lines.append(f"**Touched but no id could be read ({len(summary.unresolved)}):**")
        lines.extend(f"- `{p}`" for p in summary.unresolved)
        lines.append("")

    if not findings:
        lines.append("Validator: no findings scoped to this diff.")
    else:
        errors = [f for f in findings if f.level == "error"]
        warnings = [f for f in findings if f.level == "warning"]
        lines.append(f"Validator: {len(errors)} error(s), {len(warnings)} warning(s) "
                      f"scoped to this diff.")
        lines.append("")
        for f in errors + warnings:
            tag = "ERROR" if f.level == "error" else "WARN"
            where = f.id or f.file or "?"
            lines.append(f"- **{tag}** `{f.code}` — {where}: {f.message}")
    return "\n".join(lines) + "\n"


def annotate(base: str, *, root: Path = ROOT) -> str:
    """Build the full report: load canon once, diff, validate, filter, render.

    Appends the "## Impact" section (reverse-mentions impact analysis) so the one PR
    comment this powers (see .github/workflows/pr-validation.yml) covers both "what
    does the validator say about this diff" and "what does this diff affect elsewhere"
    with zero workflow changes. `tools.impact` is imported lazily here, not at module
    scope: it imports this module's own diff-summarization machinery (reuse, not a
    second `git diff` parser per its module docstring), so importing it back at the top
    of this file would be a cycle.
    """
    canon = load_canon(root / "canon")
    summary = summarize_diff(base, root=root, canon=canon)
    rep = validate(canon)
    touched_ids = set(summary.new_ids) | set(summary.changed_ids)
    findings = filter_findings(rep.findings, touched_ids, summary.touched_files)
    report = render_report(base, summary, findings)

    from .impact import build_impact, render_markdown
    impact = build_impact(base, root=root, canon=canon, summary=summary)
    report += "\n" + render_markdown(base, impact)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR-scoped canon diff + validator findings, as a Markdown report.")
    parser.add_argument("--base", default="origin/main",
                        help="ref to diff canon/ against (default: origin/main)")
    args = parser.parse_args(argv)

    try:
        report = annotate(args.base)
    except BaseRefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

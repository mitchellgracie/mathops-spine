---
# Seed frontmatter for raw material. Copy this file into the right raw/ bucket (see
# below), fill in what you know, delete the rest — every key is optional. The leading
# underscore keeps THIS file out of the pipeline (`extract-plan` refuses `_*` names);
# your copy must not start with one. Then: make extract-plan FILE=raw/<bucket>/<slug>.md
#
# WHERE TO PUT IT — the raw/ inbox is bucketed by what the material is:
#   raw/papers/     your own writeups of literature — statements, definitions,
#                   proof sketches, with citations; never the PDFs   (kind: notes)
#   raw/sessions/   transcripts of research conversations / working
#                   sessions                                          (kind: transcript)
#   raw/drafts/     your own prose bound for an exposition            (kind: prose)
#   raw/extracted/  post-apply archive — do not author here
# The bucket sets `kind` for you, so you can usually omit it. Dropping a file at raw/
# root still works and defaults to prose.
#
# kind: what the material IS (overrides the bucket default; a mismatch is warned about).
#   prose      — exposition-quality writing destined for expositions/; anchors matter
#                (they become [[id]] wiki-links).
#   notes      — reference material (a paper writeup, research notes); facts are
#                extracted but no exposition is emitted and anchors are neither
#                required nor checked.
#   transcript — a transcribed working session; behaves like notes (no exposition),
#                but records that the claims came from conversation, not a source.
kind: notes

# tags: topical labels (the material-tag vocabulary in tools/taxonomy.py), threaded onto
# the plan and any emitted exposition for retrieval — "every computation discussion",
# etc. Use the canonical short tags; unknown tags are kept but warned about (a nudge
# toward one spelling). Current set: literature, background, mainline, conjecture,
# computation, examples, technique, notation, writeup.
tags: [literature]

# collection / title: where a prose draft's exposition lands —
# expositions/<collection>/NNN-<title>.md (defaults: collection = filename slug,
# title = first H1). Irrelevant for notes/transcripts.
collection: survey
title: My Writeup

# hints: display name -> EXISTING entity id. Ground truth for matching — use when the
# text calls an existing entity something the candidate index won't connect
# ("Theorem 3.2", "the main bound", a paper's own numbering).
hints:
  "Theorem 3.2": thm.example

# context: free-form author ground truth the text itself doesn't state. The Extractor
# treats it as authoritative and cites facts drawn from it as `AUTHOR:` evidence lines,
# so triage can tell text-evidenced findings from author-stated ones. Best contents:
#   - which case/setting the project actually cares about ("we only need char p > 2")
#   - what a bare symbol in the text refers to
#   - known errata in the source ("the published constant is wrong; use the arXiv v3")
#   - which named results are already in canon under which ids
context: |
  This writeup covers sections 3-4 of the paper only.
  "The main theorem" here means what canon records as thm.example.
---
# Title of the material

The writeup, transcript, or draft goes here, below the frontmatter. For a paper
writeup: transcribe statements faithfully (hypotheses intact, LaTeX as written), cite
the source, and sketch proof structure where you want the dependency graph to see it.

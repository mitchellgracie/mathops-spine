"""Layer 2 (exposition corpus) helpers: discover exposition files and the entity
[[id]] links inside them.

Deliberately tiny. Expositions are prose-per-file writeups under
``expositions/<collection>/NNN-slug.md`` — working notes, survey sections, paper-draft
fragments — whose primary machine-readable content is lightweight wiki-links
(``[[thm.main_bound]]``) into canon. An exposition emitted by the extraction pipeline
also carries a small YAML frontmatter block — material ``tags`` and provenance (where
the prose came from) — which is optional metadata, not a validated schema:
hand-written expositions may omit it entirely. So this module is file discovery +
link extraction + a thin read of that frontmatter, shared by the consumers that need
it: the build step (appearances + tag indices) and the validator (dangling links).
Keeping it here means all of them agree on exactly what an "exposition", a "mention",
and an exposition's tags are.

``Exposition.body`` is deliberately the *whole* file (frontmatter included). Rewriters
like ``tools.lifecycle``'s merge substitute inside ``body`` and write it straight back,
so splitting the frontmatter out of ``body`` would silently drop it on the next merge;
the frontmatter is instead parsed on demand by ``meta``/``tags``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from .common import EXPOSITIONS_DIR
from .taxonomy import normalize_tags

# The wiki-link grammar for entity references in prose — the same `<prefix>.<slug>` shape
# the canon-body mentions index uses (tools.build imports this so there is one regex).
MENTION_RE = re.compile(r"\[\[([a-z]+\.[a-z0-9_]+)\]\]")


@dataclass(frozen=True)
class Exposition:
    """One exposition file: its repo-relative path and full file body."""

    path: str  # POSIX, relative to the expositions dir's parent (e.g. "expositions/survey/001-a.md")
    body: str  # the whole file, frontmatter included — see the module docstring

    @property
    def mentions(self) -> list[str]:
        """The distinct entity ids this exposition wiki-links, sorted for determinism."""
        return sorted(set(MENTION_RE.findall(self.body)))

    @property
    def meta(self) -> dict:
        """The exposition's frontmatter as a dict ({} for a frontmatter-less file)."""
        return dict(frontmatter.loads(self.body).metadata)

    @property
    def tags(self) -> list[str]:
        """The exposition's material tags, normalized (empty if untagged). See tools.taxonomy."""
        return normalize_tags(self.meta.get("tags"))


def load_expositions(expositions_dir: Path = EXPOSITIONS_DIR) -> list[Exposition]:
    """Load every exposition under ``<expositions_dir>/*/*.md``, sorted by path.

    Paths are stored relative to ``expositions_dir.parent`` so they read as stable
    ``expositions/<collection>/...`` strings regardless of the absolute checkout
    location (and so tests can point at a temp dir). Returns an empty list if the
    expositions tree does not exist yet — the layer is optional.
    """
    expositions_dir = Path(expositions_dir)
    if not expositions_dir.exists():
        return []
    base = expositions_dir.parent
    out = []
    for path in sorted(expositions_dir.glob("*/*.md")):
        out.append(Exposition(path.relative_to(base).as_posix(), path.read_text()))
    return out

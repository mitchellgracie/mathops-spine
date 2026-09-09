"""Build the entity knowledge graph from canon (typed reference fields + relations).

Both the build step (to serialize derived/graph.json) and the context assembler
(to compute k-hop neighborhoods) use this. Edges are directed and labelled; for
neighborhood queries we use the undirected projection.
"""
from __future__ import annotations

import networkx as nx

from schemas.introspect import ref_fields
from schemas.registry import model_for_type

from .loader import Canon


def build_graph(canon: Canon) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for eid, e in canon.entities.items():
        g.add_node(eid, type=e.type, name=e.name)

    for eid, e in canon.entities.items():
        model = model_for_type(e.type)
        for rf in ref_fields(model):
            value = getattr(e, rf.field)
            targets = value if rf.is_list else ([value] if value is not None else [])
            for t in targets:
                if t in canon.entities:
                    g.add_edge(eid, t, label=rf.field)
        for rel in e.relations:
            if rel.target in canon.entities:
                g.add_edge(eid, rel.target, label=rel.type)
    return g


def neighborhood(g: nx.MultiDiGraph, node: str, k: int) -> dict[str, int]:
    """Return {entity_id: hop_distance} for all nodes within k hops of `node`
    on the undirected projection (distance 0 is the node itself)."""
    if node not in g:
        return {}
    undirected = g.to_undirected(as_view=True)
    return nx.single_source_shortest_path_length(undirected, node, cutoff=k)

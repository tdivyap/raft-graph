"""In-memory graph store over the structural layer.

Builds hash-map indices once at construction so the queries Layer 2 needs are
O(1) lookups, not O(N) scans:

    id  -> Entity                      (get_entity)
    source_id -> [Relation]            (relations_from)
    target_id -> [Relation]            (relations_to)
    kind -> [Entity]                   (entities_of_kind)

C++ analogy: an unordered_map for id->entity plus two multimap-style adjacency
indices over the relation list.

External embeds (target_type_text, no target_id) appear in relations_from of
their source but never in relations_to -- there is no internal entity to key
them under. That is correct: they point outside the extracted package.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..structural.schema import Entity, GraphDocument, Relation


class GraphStore:
    def __init__(self, doc: GraphDocument) -> None:
        self.schema_version = doc.schema_version
        self.package = doc.package
        self.entities: list[Entity] = list(doc.entities)
        self.relations: list[Relation] = list(doc.relations)

        self._by_id: dict[str, Entity] = {e.id: e for e in doc.entities}
        self._by_kind: dict[str, list[Entity]] = defaultdict(list)
        self._out: dict[str, list[Relation]] = defaultdict(list)
        self._in: dict[str, list[Relation]] = defaultdict(list)

        for e in doc.entities:
            self._by_kind[e.kind].append(e)
        for r in doc.relations:
            self._out[r.source_id].append(r)
            target_id = getattr(r, "target_id", None)
            if target_id is not None:
                self._in[target_id].append(r)

    # -- entity access -----------------------------------------------------
    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self._by_id.get(entity_id)

    def entities_of_kind(self, kind: str) -> list[Entity]:
        return list(self._by_kind.get(kind, []))

    # -- adjacency ---------------------------------------------------------
    def relations_from(self, entity_id: str, kind: Optional[str] = None) -> list[Relation]:
        rels = self._out.get(entity_id, [])
        return [r for r in rels if kind is None or r.kind == kind]

    def relations_to(self, entity_id: str, kind: Optional[str] = None) -> list[Relation]:
        rels = self._in.get(entity_id, [])
        return [r for r in rels if kind is None or r.kind == kind]

    # -- convenience built from the four primitives ------------------------
    def neighbors_from(self, entity_id: str, kind: Optional[str] = None) -> list[Entity]:
        """Resolve outgoing edges' internal targets to Entity objects."""
        out = []
        for r in self.relations_from(entity_id, kind):
            tid = getattr(r, "target_id", None)
            if tid is not None and tid in self._by_id:
                out.append(self._by_id[tid])
        return out

    def __repr__(self) -> str:
        return (f"GraphStore(package={self.package!r}, "
                f"entities={len(self.entities)}, relations={len(self.relations)})")

"""Typed schema for the raft-graph structural layer (Layer 1 output).

Mirrors the JSON emitted by tools/ast_walker/main.go (schema_version "0.1").
These models are the validated representation of the *deterministic* structural
extraction -- the ground truth the semantic layer (Layer 2) annotates but never
invents.

Modeling stance
---------------
* Entities and relations are discriminated unions keyed on ``kind``.
  (Think ``std::variant<...>`` with ``kind`` as the type tag, rather than one
  struct full of ``std::optional`` members.)
* Go ``*T`` + ``omitempty`` maps to ``Optional[T]`` (i.e. ``std::optional<T>``):
  absent is legal, so these default to ``None``.
* ``extra="forbid"``: an *unknown* field raises. Asymmetric on purpose -- we
  tolerate omitted optionals but fail loudly on schema drift, so the first real
  load doubles as a Layer-1 output check.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Base config: reject fields this schema does not model."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

class _EntityBase(_Strict):
    id: str                 # qualified name, e.g. "go.etcd.io/raft/v3.raft"
    name: str
    qualified_name: str
    package: str
    file: str
    line: int
    column: int


class StructEntity(_EntityBase):
    kind: Literal["STRUCT"] = "STRUCT"
    # *int + omitempty: present (possibly 0) for every struct.
    num_fields: Optional[int] = None


class InterfaceEntity(_EntityBase):
    kind: Literal["INTERFACE"] = "INTERFACE"
    num_methods: Optional[int] = None


class FunctionEntity(_EntityBase):
    kind: Literal["FUNCTION"] = "FUNCTION"
    signature: Optional[str] = None


class MethodEntity(_EntityBase):
    kind: Literal["METHOD"] = "METHOD"
    signature: Optional[str] = None
    receiver_type: Optional[str] = None


class FieldEntity(_EntityBase):
    kind: Literal["FIELD"] = "FIELD"
    field_type: Optional[str] = None


class TypeAliasEntity(_EntityBase):
    kind: Literal["TYPE_ALIAS"] = "TYPE_ALIAS"
    underlying_type: Optional[str] = None
    alias_kind: Optional[Literal["named_type", "type_alias"]] = None


Entity = Annotated[
    Union[
        StructEntity,
        InterfaceEntity,
        FunctionEntity,
        MethodEntity,
        FieldEntity,
        TypeAliasEntity,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------

class _RelationBase(_Strict):
    source_id: str
    file: Optional[str] = None   # where the edge is declared, when known
    line: Optional[int] = None


class HasFieldRelation(_RelationBase):
    kind: Literal["HAS_FIELD"] = "HAS_FIELD"
    target_id: str


class HasMethodRelation(_RelationBase):
    kind: Literal["HAS_METHOD"] = "HAS_METHOD"
    target_id: str


class EmbedsRelation(_RelationBase):
    kind: Literal["EMBEDS"] = "EMBEDS"
    # Internal embed -> target_id; external (sync.Mutex, *log.Logger) ->
    # target_type_text. Exactly one is present.
    target_id: Optional[str] = None
    target_type_text: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "EmbedsRelation":
        has_id = self.target_id is not None
        has_text = self.target_type_text is not None
        if has_id == has_text:
            raise ValueError(
                "EMBEDS requires exactly one of target_id / target_type_text "
                f"(got target_id={self.target_id!r}, "
                f"target_type_text={self.target_type_text!r})"
            )
        return self


class ImplementsRelation(_RelationBase):
    kind: Literal["IMPLEMENTS"] = "IMPLEMENTS"
    target_id: str
    # Most Go methods have pointer receivers, so the implementing type is
    # typically *S. True when the check used types.NewPointer(s).
    source_is_pointer: Optional[bool] = None


class CallsRelation(_RelationBase):
    kind: Literal["CALLS"] = "CALLS"
    # source_id = enclosing function/method; target_id = callee.
    # Only internal callees (resolvable to an entity in this package) are
    # emitted, so every CALLS edge points at a real entity -- external calls
    # (fmt, other packages) are deliberately dropped for now. file/line mark
    # the call site. Edges are deduplicated per (source_id, target_id).
    target_id: str


Relation = Annotated[
    Union[
        HasFieldRelation,
        HasMethodRelation,
        EmbedsRelation,
        ImplementsRelation,
        CallsRelation,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------

class GraphDocument(_Strict):
    """The top-level shape of raft_graph.json."""

    schema_version: str
    package: str
    entities: list[Entity]
    relations: list[Relation]
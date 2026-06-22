"""Prompt templates for Layer 2 semantic interpretation.

Kept separate from extractor.py so prompts are reviewable/diffable on their own.
The contract these prompts enforce: the model interprets the entities it is
given and cites their exact `id`s; it must not introduce entities, methods, or
fields that are absent from the provided slice. That constraint is what the
grounding check downstream verifies.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "You are interpreting a Go codebase's structural knowledge graph. "
    "Every entity you are given was extracted deterministically from source "
    "(compiler-grade, via go/ast + go/types), and carries an exact `id` and "
    "file:line provenance. Your job is SEMANTIC interpretation only: explain "
    "what each method promises and how the implementation satisfies it.\n\n"
    "Hard rules:\n"
    "1. Do NOT invent entities, methods, fields, or ids. Reference only the "
    "entities provided below, by their exact `id`.\n"
    "2. For every entity you reference, include its exact `id` string.\n"
    "3. Base every claim on the provided entities -- their names, signatures, "
    "types, and the relations between them. You MAY use general domain "
    "knowledge to explain what a method PROMISES (its contract). You must NOT "
    "describe how an implementation works internally -- which fields it reads "
    "or writes, what error values it returns, what other functions it calls, "
    "its control flow -- unless that information is present in the provided "
    "entities.\n"
    "4. Determining internal behavior usually requires the method body, which "
    "is NOT in this graph. When a claim would require the body, state that it "
    "is not determinable from the provided entities rather than describing the "
    "likely mechanism.\n"
    "5. Output ONLY a single JSON object in the schema specified. No prose "
    "outside the JSON, no markdown code fences."
)


def _entity_view(e: Any) -> dict:
    """Compact, provenance-bearing view of an entity for the prompt."""
    view = {
        "id": e.id,
        "kind": e.kind,
        "name": e.name,
        "file_line": f"{e.file.split('/')[-1]}:{e.line}",
    }
    for attr in ("signature", "receiver_type", "field_type", "num_fields"):
        val = getattr(e, attr, None)
        if val is not None:
            view[attr] = val
    return view


def render_user_prompt(
    interface_entity: Any,
    interface_methods: list[Any],
    impl_entity: Any,
    impl_methods: list[Any],
) -> str:
    """Build the user message for the interface-contract experiment."""
    payload = {
        "interface": _entity_view(interface_entity),
        "interface_methods": [_entity_view(m) for m in interface_methods],
        "implementation": _entity_view(impl_entity),
        "implementation_methods": [_entity_view(m) for m in impl_methods],
    }

    output_schema = {
        "methods": [
            {
                "interface_method_id": "<exact id of the interface method>",
                "semantic_contract": "<what this method promises / invariants>",
                "fulfilled_by_id": "<exact id of the implementation method that satisfies it>",
                "fulfillment_explanation": "<what the provided entities establish about how it satisfies the contract, e.g. matching name and signature; do NOT describe body internals absent from the entities>",
                "cited_entity_ids": ["<every entity id referenced in the two text fields above>"],
            }
        ]
    }

    return (
        f"Here is the interface `{interface_entity.name}` and its methods, "
        f"plus `{impl_entity.name}` (which implements it) and ITS methods. "
        "Note the implementation may expose more methods than the interface "
        "requires; map each interface method to the implementation method that "
        "actually satisfies it.\n\n"
        f"GRAPH SLICE:\n{json.dumps(payload, indent=2)}\n\n"
        "For EACH interface method, produce an object with these fields:\n"
        f"{json.dumps(output_schema, indent=2)}\n\n"
        "Return the JSON object only."
    )

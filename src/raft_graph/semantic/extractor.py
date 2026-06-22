"""Layer 2 semantic interpretation, with structural grounding verification.

Flow:
  1. assemble_interface_slice  -- pull interface + methods + implementer from
     the structural graph (retrieval).
  2. structural_fulfillment_map -- deterministically resolve which impl method
     satisfies each interface method (name+signature; the answer key).
  3. run_interpretation        -- hand the slice to the LLM, ask for semantic
     contracts (generation).
  4. check_grounding           -- verify the LLM's output against the store and
     the answer key. This is the thesis as a runnable assertion: the LLM may
     interpret, but every structural claim it makes is checked against the
     compiler-derived graph.

The LLM call reads ANTHROPIC_API_KEY from the environment. It is never passed
in code. Use dry_run=True to assemble the prompt without spending a token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..graph.store import GraphStore
from . import llm, prompts


# ---------------------------------------------------------------------------
# 1. Retrieval: assemble the slice
# ---------------------------------------------------------------------------

@dataclass
class InterfaceSlice:
    interface: Any
    interface_methods: list[Any]
    impl: Any
    impl_methods: list[Any]


def assemble_interface_slice(store: GraphStore, interface_id: str) -> InterfaceSlice:
    """Pull an interface, its methods, its implementer, and the impl's methods."""
    interface = store.get_entity(interface_id)
    if interface is None or interface.kind != "INTERFACE":
        raise ValueError(f"{interface_id!r} is not an INTERFACE in the graph")

    iface_methods = [
        store.get_entity(r.target_id)
        for r in store.relations_from(interface_id, kind="HAS_METHOD")
    ]

    impl_edges = store.relations_to(interface_id, kind="IMPLEMENTS")
    if not impl_edges:
        raise ValueError(f"no IMPLEMENTS edge targets {interface_id!r}")
    impl = store.get_entity(impl_edges[0].source_id)  # first implementer
    impl_methods = [
        store.get_entity(r.target_id)
        for r in store.relations_from(impl.id, kind="HAS_METHOD")
    ]

    return InterfaceSlice(interface, iface_methods, impl, impl_methods)


# ---------------------------------------------------------------------------
# 2. Deterministic answer key
# ---------------------------------------------------------------------------

def structural_fulfillment_map(slice_: InterfaceSlice) -> dict[str, str]:
    """interface_method_id -> impl_method_id, resolved by name.

    Go interface satisfaction is by method name (+signature); the IMPLEMENTS
    edge guarantees a same-named impl method exists for each interface method.
    This is the compiler-grade ground truth the LLM is graded against.
    """
    impl_by_name = {m.name: m for m in slice_.impl_methods}
    mapping: dict[str, str] = {}
    for im in slice_.interface_methods:
        impl_m = impl_by_name.get(im.name)
        if impl_m is None:
            raise ValueError(
                f"no impl method named {im.name!r} on {slice_.impl.name} "
                "-- IMPLEMENTS edge and method set disagree"
            )
        mapping[im.id] = impl_m.id
    return mapping


# ---------------------------------------------------------------------------
# 3. Generation: call the model
# ---------------------------------------------------------------------------

def run_interpretation(
    store: GraphStore,
    interface_id: str,
    provider: str = "auto",
    model: Optional[str] = None,
    dry_run: bool = False,
    max_tokens: int = 8192,
) -> "ExperimentResult":
    slice_ = assemble_interface_slice(store, interface_id)
    answer_key = structural_fulfillment_map(slice_)
    user_prompt = prompts.render_user_prompt(
        slice_.interface, slice_.interface_methods, slice_.impl, slice_.impl_methods
    )

    if dry_run:
        return ExperimentResult(slice_=slice_, answer_key=answer_key,
                                user_prompt=user_prompt, raw_response=None,
                                parsed=None, report=None)

    raw = llm.complete(
        prompts.SYSTEM_PROMPT, user_prompt,
        provider=provider, model=model, max_tokens=max_tokens,
    )
    parsed = _parse_json(raw)
    report = check_grounding(parsed, store, slice_, answer_key)
    return ExperimentResult(slice_=slice_, answer_key=answer_key,
                            user_prompt=user_prompt, raw_response=raw,
                            parsed=parsed, report=report)


def _parse_json(text: str) -> dict:
    """Tolerate stray code fences; otherwise strict JSON."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError as exc:
        snippet = text[:800] if text.strip() else "<empty response>"
        raise ValueError(
            f"model did not return valid JSON ({exc}).\n--- raw response ---\n{snippet}"
        ) from exc


# ---------------------------------------------------------------------------
# 4. The grounding check -- the thesis as an assertion
# ---------------------------------------------------------------------------

@dataclass
class GroundingReport:
    grounded: bool
    expected_methods: list[str]
    returned_methods: list[str]
    missing_methods: list[str]      # contract methods the LLM omitted
    invented_methods: list[str]     # interface method ids that don't exist
    violations: list[dict]          # per-method grounding failures

    def summary(self) -> str:
        lines = [f"grounded = {self.grounded}"]
        if self.missing_methods:
            lines.append(f"  missing interface methods: {self.missing_methods}")
        if self.invented_methods:
            lines.append(f"  invented interface methods: {self.invented_methods}")
        for v in self.violations:
            lines.append(f"  [{v['type']}] {v['interface_method_id']} -> {v['detail']}")
        if self.grounded:
            lines.append("  every cited id exists; every fulfillment matches the compiler.")
        return "\n".join(lines)


def check_grounding(
    parsed: dict, store: GraphStore, slice_: InterfaceSlice, answer_key: dict[str, str]
) -> GroundingReport:
    expected = {m.id for m in slice_.interface_methods}
    returned = []
    invented = []
    violations: list[dict] = []

    for item in parsed.get("methods", []):
        im_id = item.get("interface_method_id")
        returned.append(im_id)

        # (a) did the LLM invent an interface method that isn't real?
        if im_id not in expected:
            invented.append(im_id)
            continue

        # (b) every cited id must resolve to a real entity
        for cid in item.get("cited_entity_ids", []):
            if store.get_entity(cid) is None:
                violations.append({
                    "interface_method_id": im_id,
                    "type": "hallucinated_citation",
                    "detail": f"cited id not in graph: {cid}",
                })

        # (c) the chosen fulfiller must exist AND match the compiler's answer
        fb = item.get("fulfilled_by_id")
        if store.get_entity(fb) is None:
            violations.append({
                "interface_method_id": im_id,
                "type": "fulfilled_by_missing",
                "detail": f"fulfilled_by_id not in graph: {fb}",
            })
        elif fb != answer_key.get(im_id):
            violations.append({
                "interface_method_id": im_id,
                "type": "fulfilled_by_mismatch",
                "detail": f"LLM said {fb}, compiler says {answer_key.get(im_id)}",
            })

    missing = sorted(expected - set(returned))
    grounded = not (missing or invented or violations)
    return GroundingReport(grounded, sorted(expected), returned,
                           missing, invented, violations)


@dataclass
class ExperimentResult:
    slice_: InterfaceSlice
    answer_key: dict[str, str]
    user_prompt: str
    raw_response: Optional[str]
    parsed: Optional[dict]
    report: Optional[GroundingReport]

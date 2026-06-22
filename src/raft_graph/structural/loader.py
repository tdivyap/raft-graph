"""Load raft_graph.json into a validated GraphDocument.

Thin by design: the schema does the work. This module's only added value is
turning a Pydantic ValidationError into a message that points at the offending
entity/relation instead of a wall of nested locations -- useful when the Go
extractor changes shape and you want to know *what* drifted, fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from pydantic import ValidationError

from .schema import GraphDocument


class GraphLoadError(Exception):
    """Raised when raft_graph.json does not match the structural schema."""


def load_graph_document(path: Union[str, Path]) -> GraphDocument:
    """Read and validate raft_graph.json.

    Raises GraphLoadError with a compact, located summary on validation
    failure (e.g. schema drift in the Go extractor's output).
    """
    path = Path(path)
    raw = json.loads(path.read_text())
    try:
        return GraphDocument.model_validate(raw)
    except ValidationError as exc:
        raise GraphLoadError(_summarize(path, raw, exc)) from exc


def _summarize(path: Path, raw: dict, exc: ValidationError) -> str:
    """One readable line per error, naming the offending entity/relation id."""
    lines = [f"{len(exc.errors())} validation error(s) in {path.name}:"]
    for err in exc.errors()[:25]:
        loc = err["loc"]
        ref = _locate(raw, loc)
        lines.append(f"  [{err['type']}] {'.'.join(map(str, loc))} -- {err['msg']}{ref}")
    remaining = len(exc.errors()) - 25
    if remaining > 0:
        lines.append(f"  ... and {remaining} more")
    return "\n".join(lines)


def _locate(raw: dict, loc: tuple) -> str:
    """Best-effort: map an error location back to an entity/relation id."""
    if len(loc) >= 2 and loc[0] in ("entities", "relations") and isinstance(loc[1], int):
        item = raw.get(loc[0], [])[loc[1]] if loc[1] < len(raw.get(loc[0], [])) else {}
        ident = item.get("id") or item.get("source_id") or "?"
        return f"  (in {loc[0]}[{loc[1]}] id={ident})"
    return ""

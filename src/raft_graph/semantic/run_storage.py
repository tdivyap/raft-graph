"""Run the interface-contract experiment end to end.

    uv run python -m raft_graph.semantic.run_storage
    uv run python -m raft_graph.semantic.run_storage go.etcd.io/raft/v3.Logger
    uv run python -m raft_graph.semantic.run_storage <interface_id> <path/to/raft_graph.json>

Reads the structural graph, runs the semantic interpretation (provider/model
auto-detected from your .env), and prints the grounding report plus the raw
model response. Exit code is 0 when grounded, 1 otherwise -- so it doubles as a
CI check.
"""

from __future__ import annotations

import sys

from ..graph.store import GraphStore
from ..structural.loader import load_graph_document
from .extractor import run_interpretation

DEFAULT_INTERFACE = "go.etcd.io/raft/v3.Storage"
DEFAULT_DATA = "data/raft_graph.json"


def main() -> int:
    interface_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INTERFACE
    data_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DATA

    store = GraphStore(load_graph_document(data_path))
    print(f"loaded: {store}")
    print(f"interface: {interface_id}\n")

    result = run_interpretation(store, interface_id)

    print("=== grounding report ===")
    print(result.report.summary())

    from .render import write_result_html
    path = write_result_html(result)
    print(f"\nwrote {path}  (open it in a browser)")

    return 0 if result.report.grounded else 1


if __name__ == "__main__":
    raise SystemExit(main())

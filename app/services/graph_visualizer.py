from __future__ import annotations

import html
import json
from typing import Any


def graph_to_pyvis_html(graph: dict[str, list[dict[str, Any]]], height: str = "720px") -> str:
    """Render graph data with PyVis when available, otherwise use a small HTML fallback."""
    try:
        from pyvis.network import Network
    except ImportError:
        return graph_to_simple_html(graph)

    network = Network(height=height, width="100%", directed=True, bgcolor="#fbfaf5", font_color="#202018")
    network.barnes_hut(gravity=-6000, central_gravity=0.25, spring_length=180)

    for node in graph.get("nodes", []):
        node_id = str(node.get("id"))
        labels = [label for label in node.get("labels", []) if label != "Entity"]
        title = _metadata_title(node)
        network.add_node(
            node_id,
            label=node.get("name") or node_id,
            title=title,
            group=labels[0] if labels else "Entity",
        )

    for edge in graph.get("edges", []):
        network.add_edge(
            str(edge.get("source")),
            str(edge.get("target")),
            label=edge.get("type", ""),
            title=_metadata_title(edge),
            arrows="to",
        )

    return network.generate_html(notebook=False)


def graph_to_simple_html(graph: dict[str, list[dict[str, Any]]]) -> str:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    node_items = "\n".join(
        f"<li><strong>{html.escape(str(node.get('name') or node.get('id')))}</strong> "
        f"<code>{html.escape(', '.join(node.get('labels', [])))}</code>"
        f"<p>{html.escape(str(node.get('source_snippet', '')))}</p></li>"
        for node in nodes
    )
    edge_items = "\n".join(
        f"<li>{html.escape(str(edge.get('source')))} "
        f"<strong>{html.escape(str(edge.get('type', 'RELATED_TO')))}</strong> "
        f"{html.escape(str(edge.get('target')))}"
        f"<p>{html.escape(str(edge.get('source_snippet', '')))}</p></li>"
        for edge in edges
    )
    payload = html.escape(json.dumps(graph, indent=2, ensure_ascii=False))
    return f"""
    <section style="font-family: Georgia, serif; background:#fbfaf5; padding:1rem;">
      <h2>Graph Snapshot</h2>
      <h3>Nodes</h3>
      <ul>{node_items}</ul>
      <h3>Relations</h3>
      <ul>{edge_items}</ul>
      <details><summary>Raw metadata</summary><pre>{payload}</pre></details>
    </section>
    """


def _metadata_title(record: dict[str, Any]) -> str:
    payload = {
        "id": record.get("id"),
        "labels": record.get("labels"),
        "type": record.get("type"),
        "snippet": record.get("source_snippet"),
        "provenance": record.get("provenance"),
        "metadata": record.get("metadata"),
    }
    return html.escape(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

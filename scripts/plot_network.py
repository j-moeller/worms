#!/usr/bin/env python3

import argparse
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.lines import Line2D

LAYER_ORDER = {
    "wan": 0,
    "edge": 1,
    "core": 2,
    "distribution": 3,
    "access": 4,
    "endpoint": 5,
}

# Network types from the generator.
NETWORK_TYPE_MARKERS = {
    "users": "o",
    "servers": "s",
    "wifi": "^",
    "voice": "D",
    "iot": "P",
    "storage": "X",
    "management": "v",
    "dmz": "h",
    "transit": "*",
}


def load_graph(filename: str) -> tuple[dict, nx.DiGraph]:
    """
    Load the network-file JSON produced by ArchitectureGenerator.
    """

    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)

    graph = nx.DiGraph()

    for node in data.get("nodes", []):
        graph.add_node(
            node["id"],
            **node,
        )

    for edge in data.get("edges", []):
        graph.add_edge(
            edge["source"],
            edge["target"],
            **edge,
        )

    return data, graph


def node_color(node: dict) -> str:
    """
    Select a color based on the logical layer.
    """

    layer = node.get("layer")

    colors = {
        "wan": "#4c78a8",
        "edge": "#f58518",
        "core": "#e45756",
        "distribution": "#72b7b2",
        "access": "#54a24b",
        "endpoint": "#b279a2",
    }

    return colors.get(layer, "#999999")


def node_size(node: dict) -> int:
    """
    Make infrastructure nodes larger than endpoints.
    """

    node_type = node.get("type")

    sizes = {
        "external": 1800,
        "router-firewall": 1500,
        "router": 1400,
        "switch": 1100,
        "network": 850,
        "virtual-machine": 500,
    }

    return sizes.get(node_type, 700)


def node_shape(node: dict) -> str:
    """
    Choose a Matplotlib marker based on node type.
    """

    node_type = node.get("type")

    shapes = {
        "external": "*",
        "router-firewall": "D",
        "router": "D",
        "switch": "s",
        "network": "o",
        "virtual-machine": "o",
    }

    return shapes.get(node_type, "o")


def edge_style(edge: dict) -> dict:
    """
    Style edges according to their relationship.
    """

    relationship = edge.get("relationship")

    styles = {
        "wan-uplink": {
            "style": "dashed",
            "width": 2.5,
        },
        "edge-core": {
            "style": "solid",
            "width": 2.2,
        },
        "core-distribution": {
            "style": "solid",
            "width": 1.8,
        },
        "distribution-access": {
            "style": "solid",
            "width": 1.5,
        },
        "network-segment": {
            "style": "dotted",
            "width": 1.8,
        },
        "attached-to": {
            "style": "dashed",
            "width": 1.0,
        },
    }

    return styles.get(
        relationship,
        {
            "style": "solid",
            "width": 1.2,
        },
    )


def build_hierarchical_positions(graph: nx.DiGraph) -> dict:
    """
    Build deterministic positions based on:

        site
          |
          +-- wan
          +-- edge
          +-- core
          +-- distribution
          +-- access
          +-- network
          +-- endpoint

    Sites are placed next to one another horizontally.
    """

    positions = {}

    sites = sorted(
        {
            node_data.get("site")
            for _, node_data in graph.nodes(data=True)
            if node_data.get("site") and node_data.get("site") != "global"
        }
    )

    # Global WAN node.
    if "internet" in graph:
        positions["internet"] = (0, 0)

    # Width allocated to each site.
    site_spacing = 20

    for site_index, site in enumerate(sites):
        site_x = (site_index - (len(sites) - 1) / 2) * site_spacing

        site_nodes = [
            (node_id, data)
            for node_id, data in graph.nodes(data=True)
            if data.get("site") == site
        ]

        # Group nodes by logical layer.
        layers = defaultdict(list)

        for node_id, data in site_nodes:
            layer = data.get("layer", "unknown")

            # Network segments are logically below access.
            if data.get("type") == "network":
                layer = "network"

            layers[layer].append((node_id, data))

        ordered_layers = [
            "edge",
            "core",
            "distribution",
            "access",
            "network",
            "endpoint",
        ]

        y_positions = {
            "edge": 7,
            "core": 5,
            "distribution": 3,
            "access": 1,
            "network": -1,
            "endpoint": -3,
        }

        for layer in ordered_layers:
            nodes = layers.get(layer, [])

            if not nodes:
                continue

            # Center nodes belonging to this layer around the site's x.
            spacing = 2.4

            start_x = site_x - ((len(nodes) - 1) * spacing) / 2

            for index, (node_id, _) in enumerate(nodes):
                positions[node_id] = (
                    start_x + index * spacing,
                    y_positions[layer],
                )

    # Make Internet sit above all sites.
    if "internet" in positions:
        positions["internet"] = (
            0,
            10,
        )

    # Fallback for anything not positioned above.
    unpositioned = [node_id for node_id in graph.nodes if node_id not in positions]

    if unpositioned:
        fallback = nx.spring_layout(
            graph.subgraph(unpositioned),
            seed=42,
        )

        for node_id, position in fallback.items():
            positions[node_id] = (
                position[0] * 5,
                position[1] * 5,
            )

    return positions


def draw_site_boundaries(
    graph: nx.DiGraph,
    positions: dict,
    ax,
) -> None:
    """
    Draw a light bounding box around each site.
    """

    sites = sorted(
        {
            data.get("site")
            for _, data in graph.nodes(data=True)
            if data.get("site") and data.get("site") != "global"
        }
    )

    for site in sites:
        site_nodes = [
            node_id
            for node_id, data in graph.nodes(data=True)
            if data.get("site") == site and node_id in positions
        ]

        if not site_nodes:
            continue

        xs = [positions[node][0] for node in site_nodes]
        ys = [positions[node][1] for node in site_nodes]

        min_x = min(xs) - 2.0
        max_x = max(xs) + 2.0
        min_y = min(ys) - 1.3
        max_y = max(ys) + 1.3

        rectangle = plt.Rectangle(
            (min_x, min_y),
            max_x - min_x,
            max_y - min_y,
            fill=False,
            linestyle="--",
            linewidth=1.0,
            alpha=0.35,
        )

        ax.add_patch(rectangle)

        ax.text(
            (min_x + max_x) / 2,
            max_y + 0.3,
            site,
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )


def draw_graph(
    data: dict,
    graph: nx.DiGraph,
    output: str | None = None,
    show_labels: bool = True,
) -> None:
    """
    Render the topology.
    """

    positions = build_hierarchical_positions(graph)

    # Scale the canvas with the number of nodes.
    node_count = len(graph.nodes)

    width = max(
        16,
        min(30, 12 + node_count / 8),
    )

    height = max(
        10,
        min(20, 10 + node_count / 20),
    )

    fig, ax = plt.subplots(
        figsize=(width, height),
    )

    draw_site_boundaries(
        graph,
        positions,
        ax,
    )

    # Draw edges one relationship type at a time so their styles
    # remain distinguishable.
    for relationship in sorted(
        {edge_data.get("relationship") for _, _, edge_data in graph.edges(data=True)}
    ):
        edges = [
            (source, target)
            for source, target, edge_data in graph.edges(data=True)
            if edge_data.get("relationship") == relationship
        ]

        if not edges:
            continue

        style = edge_style(
            {
                "relationship": relationship,
            }
        )

        nx.draw_networkx_edges(
            graph,
            positions,
            edgelist=edges,
            ax=ax,
            arrows=True,
            arrowstyle="-|>",
            arrowsize=12,
            edge_color="#777777",
            width=style["width"],
            style=style["style"],
            alpha=0.75,
            connectionstyle="arc3,rad=0.03",
        )

    # Draw nodes grouped by node type so marker shapes work.
    node_types = sorted({data.get("type") for _, data in graph.nodes(data=True)})

    for node_type in node_types:
        nodes = [
            node_id
            for node_id, data in graph.nodes(data=True)
            if data.get("type") == node_type
        ]

        if not nodes:
            continue

        nx.draw_networkx_nodes(
            graph,
            positions,
            nodelist=nodes,
            ax=ax,
            node_color=[node_color(graph.nodes[node_id]) for node_id in nodes],
            node_size=[node_size(graph.nodes[node_id]) for node_id in nodes],
            node_shape=node_shape(graph.nodes[nodes[0]]),
            edgecolors="black",
            linewidths=1.0,
            alpha=0.95,
        )

    if show_labels:
        labels = {}

        for node_id, node in graph.nodes(data=True):
            node_type = node.get("type")

            if node_type == "virtual-machine":
                # Keep VM labels compact.
                label = node.get("label", node_id)
                label = label.replace("sandbox-", "")
            elif node_type == "network":
                label = (
                    f"{node.get('label', node_id)}\n"
                    f"{node.get('cidr', '')}\n"
                    f"{node.get('network_type', '')}"
                )
            else:
                label = node.get("label", node_id)

            labels[node_id] = label

        nx.draw_networkx_labels(
            graph,
            positions,
            labels=labels,
            ax=ax,
            font_size=7,
            font_weight="normal",
            verticalalignment="bottom",
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.75,
            },
        )

    # Layer labels on the left.
    layer_labels = [
        ("wan", 10),
        ("edge", 7),
        ("core", 5),
        ("distribution", 3),
        ("access", 1),
        ("network", -1),
        ("endpoint", -3),
    ]

    for label, y in layer_labels:
        ax.text(
            -width / 2 + 0.5,
            y,
            label.upper(),
            fontsize=8,
            fontweight="bold",
            alpha=0.55,
            ha="left",
            va="center",
        )

    # Node-type legend.
    node_legend = [
        Line2D(
            [0],
            [0],
            marker=node_shape({"type": node_type}),
            color="none",
            markerfacecolor=node_color({"layer": layer}),
            markeredgecolor="black",
            markersize=9,
            label=node_type,
        )
        for node_type, layer in [
            ("external", "wan"),
            ("router-firewall", "edge"),
            ("router", "core"),
            ("switch", "distribution"),
            ("network", "access"),
            ("virtual-machine", "endpoint"),
        ]
    ]

    # Relationship legend.
    relationship_legend = []

    for relationship in [
        "wan-uplink",
        "edge-core",
        "core-distribution",
        "distribution-access",
        "network-segment",
        "attached-to",
    ]:
        style = edge_style(
            {
                "relationship": relationship,
            }
        )

        relationship_legend.append(
            Line2D(
                [0],
                [0],
                color="#777777",
                linewidth=style["width"],
                linestyle=style["style"],
                label=relationship,
            )
        )

    legend_1 = ax.legend(
        handles=node_legend,
        title="Node types",
        loc="upper left",
        bbox_to_anchor=(1.01, 1.0),
    )

    ax.add_artist(legend_1)

    ax.legend(
        handles=relationship_legend,
        title="Relationships",
        loc="upper left",
        bbox_to_anchor=(1.01, 0.58),
    )

    seed = data.get("seed", "unknown")

    ax.set_title(
        f"Enterprise Network Topology\n"
        f"seed={seed} | "
        f"{len(graph.nodes)} nodes | "
        f"{len(graph.edges)} edges",
        fontsize=15,
        fontweight="bold",
        pad=20,
    )

    ax.axis("off")

    plt.tight_layout()

    if output:
        output_path = Path(output)

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        fig.savefig(
            output_path,
            dpi=200,
            bbox_inches="tight",
        )

        print(f"Topology written to: {output_path}")

    else:
        plt.show()

    plt.close(fig)


def print_summary(data: dict, graph: nx.DiGraph) -> None:
    """
    Print a useful textual summary alongside the visualization.
    """

    nodes_by_type = defaultdict(int)
    nodes_by_layer = defaultdict(int)
    networks_by_type = defaultdict(int)

    for _, node in graph.nodes(data=True):
        nodes_by_type[node.get("type", "unknown")] += 1
        nodes_by_layer[node.get("layer", "unknown")] += 1

        if node.get("type") == "network":
            networks_by_type[node.get("network_type", "unknown")] += 1

    print()
    print("Network summary")
    print("----------------")
    print(f"Seed:       {data.get('seed', 'unknown')}")
    print(f"Nodes:      {len(graph.nodes)}")
    print(f"Edges:      {len(graph.edges)}")

    print()
    print("Nodes by type:")
    for node_type, count in sorted(nodes_by_type.items()):
        print(f"  {node_type:20} {count}")

    print()
    print("Nodes by layer:")
    for layer, count in sorted(
        nodes_by_layer.items(),
        key=lambda item: LAYER_ORDER.get(item[0], 99),
    ):
        print(f"  {layer:20} {count}")

    if networks_by_type:
        print()
        print("Network segments:")
        for network_type, count in sorted(networks_by_type.items()):
            print(f"  {network_type:20} {count}")

    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Visualize the JSON topology produced by "
            "the enterprise network generator."
        )
    )

    parser.add_argument(
        "network_file",
        help="JSON file generated with --network-file.",
    )

    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Write the visualization to an image file "
            "(PNG, SVG, PDF, etc.) instead of opening a window."
        ),
    )

    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="Do not draw node labels.",
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a topology summary before rendering.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data, graph = load_graph(args.network_file)

    if args.summary:
        print_summary(
            data,
            graph,
        )

    draw_graph(
        data=data,
        graph=graph,
        output=args.output,
        show_labels=not args.no_labels,
    )


if __name__ == "__main__":
    main()

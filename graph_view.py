"""Build and display FlowGraph views of RouteNinja results."""
from __future__ import annotations

import logging
from typing import List, Optional

from binaryninja import (
    BinaryView,
    BranchType,
    DisassemblyTextLine,
    FlowGraph,
    FlowGraphNode,
    HighlightStandardColor,
    InstructionTextToken,
    InstructionTextTokenType,
)

# Lazy import: `binaryninja.interaction` is a submodule, and some Python
# import machinery paths (notably the headless RPyC client used for tests)
# choke on resolving submodule netrefs eagerly. Defer it to the one function
# that needs it.
def _show_graph_report(title: str, graph):
    from binaryninja import interaction
    interaction.show_graph_report(title, graph)


def _show_message_box(title: str, body: str):
    from binaryninja import interaction
    interaction.show_message_box(title, body)


from RouteNinja.route_ninja import AnnotatedPath, CallTree


def _function_name(bv: BinaryView, addr: int) -> str:
    try:
        fns = bv.get_functions_at(addr)
        if fns:
            return str(fns[0].name)
    except Exception:
        pass
    try:
        sym = bv.get_symbol_at(addr)
        if sym is not None:
            name = getattr(sym, "full_name", None) or getattr(sym, "name", None)
            if name:
                return str(name)
    except Exception:
        pass
    return hex(addr)


def _node_lines(bv: BinaryView, addr: int,
                label: Optional[str] = None,
                extra: Optional[List[str]] = None) -> List[DisassemblyTextLine]:
    """Build the text lines shown in a graph node.

    The first line carries `addr` so double-clicking the node in the flow
    graph report navigates the linear view to that function. The symbol
    token is rendered with the function's color so it looks like every
    other function reference in Binja's UI.
    """
    name = _function_name(bv, addr)
    header_tokens = []
    if label:
        header_tokens.append(
            InstructionTextToken(InstructionTextTokenType.AnnotationToken, f"[{label}] ")
        )
    header_tokens.extend([
        InstructionTextToken(
            InstructionTextTokenType.CodeSymbolToken, name, addr, len(name)
        ),
        InstructionTextToken(InstructionTextTokenType.TextToken, "  "),
        InstructionTextToken(
            InstructionTextTokenType.AddressDisplayToken, hex(addr), addr
        ),
    ])
    lines = [DisassemblyTextLine(header_tokens, addr)]

    if extra:
        for ex in extra:
            lines.append(DisassemblyTextLine([
                InstructionTextToken(InstructionTextTokenType.TextToken, ex)
            ]))
    return lines


def build_single_path_graph(bv: BinaryView, path: AnnotatedPath,
                            title: str = "RouteNinja path") -> FlowGraph:
    """Render one path as a vertical chain of nodes."""
    fg = FlowGraph()
    nodes: List[FlowGraphNode] = []

    for idx, addr in enumerate(path.nodes):
        node = FlowGraphNode(fg)
        if idx == 0:
            label = "SOURCE"
        elif idx == len(path.nodes) - 1:
            label = "TARGET"
        else:
            label = None
        extra = None
        if idx < len(path.edges):
            edge = path.edges[idx]
            if edge.call_sites:
                extra = [f"calls next @ {', '.join(hex(s) for s in edge.call_sites[:4])}"]
                if len(edge.call_sites) > 4:
                    extra[0] += f" (+{len(edge.call_sites) - 4})"
        node.lines = _node_lines(bv, addr, label=label, extra=extra)
        if idx == 0:
            node.highlight = HighlightStandardColor.GreenHighlightColor
        elif idx == len(path.nodes) - 1:
            node.highlight = HighlightStandardColor.RedHighlightColor
        fg.append(node)
        nodes.append(node)

    for i in range(len(nodes) - 1):
        nodes[i].add_outgoing_edge(BranchType.UnconditionalBranch, nodes[i + 1])

    return fg


def build_multi_path_graph(bv: BinaryView, paths: List[AnnotatedPath],
                           title: str = "RouteNinja paths") -> FlowGraph:
    """Render *all* paths as one DAG — shared functions collapse to one node.

    This is the most useful view when the caller wants to see how many
    branches fan out from source and converge on target.
    """
    fg = FlowGraph()
    addr_to_node = {}
    edge_set = set()   # (u, v) to avoid duplicate edges
    source_addrs = set()
    target_addrs = set()
    inner_addrs = set()

    for p in paths:
        if not p.nodes:
            continue
        source_addrs.add(p.nodes[0])
        target_addrs.add(p.nodes[-1])
        for a in p.nodes[1:-1]:
            inner_addrs.add(a)

    # Call-site projection per edge across all paths.
    edge_call_sites = {}
    for p in paths:
        for e in p.edges:
            edge_call_sites.setdefault((e.caller, e.callee), set()).update(e.call_sites)

    def get_or_add(addr: int) -> FlowGraphNode:
        node = addr_to_node.get(addr)
        if node is not None:
            return node
        node = FlowGraphNode(fg)
        label = None
        if addr in source_addrs:
            label = "SOURCE"
        elif addr in target_addrs:
            label = "TARGET"
        node.lines = _node_lines(bv, addr, label=label)
        if addr in source_addrs and addr not in target_addrs:
            node.highlight = HighlightStandardColor.GreenHighlightColor
        elif addr in target_addrs and addr not in source_addrs:
            node.highlight = HighlightStandardColor.RedHighlightColor
        elif addr in source_addrs and addr in target_addrs:
            node.highlight = HighlightStandardColor.OrangeHighlightColor
        fg.append(node)
        addr_to_node[addr] = node
        return node

    for p in paths:
        for e in p.edges:
            u = get_or_add(e.caller)
            v = get_or_add(e.callee)
            key = (e.caller, e.callee)
            if key in edge_set:
                continue
            edge_set.add(key)
            u.add_outgoing_edge(BranchType.UnconditionalBranch, v)

    # Make sure isolated source/target appear even with no paths.
    for a in source_addrs | target_addrs:
        get_or_add(a)

    return fg


def build_call_tree_graph(bv: BinaryView, tree: CallTree) -> FlowGraph:
    """Render a CallTree as a FlowGraph.

    Root is highlighted green. Each node is annotated with its depth (as
    a small `[d=N]` marker) so the layered structure stays legible even
    when the tree spans many levels. Edges are the parent→child relations
    collected during BFS.
    """
    fg = FlowGraph()
    addr_to_node = {}

    # Truncation marker — a sentinel child we attach to every frontier
    # node that still had unexplored edges at the cutoff. Keeps users
    # from mistaking a pruned branch for a genuine leaf.
    def make_node(addr: int) -> FlowGraphNode:
        existing = addr_to_node.get(addr)
        if existing is not None:
            return existing
        node = FlowGraphNode(fg)
        d = tree.depth.get(addr, 0)
        if addr == tree.root:
            label = "ROOT"
        else:
            label = f"d={d}"
        extra = None
        node.lines = _node_lines(bv, addr, label=label, extra=extra)
        if addr == tree.root:
            node.highlight = HighlightStandardColor.GreenHighlightColor
        fg.append(node)
        addr_to_node[addr] = node
        return node

    # Ensure the root appears even when the tree has no edges.
    make_node(tree.root)

    seen_edges = set()
    for u, v in tree.edges:
        key = (u, v)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        nu = make_node(u)
        nv = make_node(v)
        nu.add_outgoing_edge(BranchType.UnconditionalBranch, nv)

    if tree.truncated:
        trunc = FlowGraphNode(fg)
        trunc.lines = [DisassemblyTextLine([
            InstructionTextToken(
                InstructionTextTokenType.AnnotationToken,
                "...truncated (increase max depth or max nodes)",
            )
        ])]
        trunc.highlight = HighlightStandardColor.YellowHighlightColor
        fg.append(trunc)

    return fg


def show_call_tree(bv: BinaryView, tree: CallTree,
                   title: Optional[str] = None) -> None:
    if not tree.nodes:
        _show_message_box("RouteNinja", "Call tree is empty.")
        return
    try:
        fg = build_call_tree_graph(bv, tree)
    except Exception:
        logging.exception("Failed to build RouteNinja call tree graph.")
        return
    if title is None:
        root_name = _function_name(bv, tree.root)
        label = "callees" if tree.direction == "callees" else "callers"
        title = f"RouteNinja call tree ({label}): {root_name}"
    try:
        _show_graph_report(title, fg)
    except Exception:
        logging.exception("Failed to display RouteNinja call tree.")


def show_paths(bv: BinaryView, paths: List[AnnotatedPath],
               title: str = "RouteNinja paths", merged: bool = True) -> None:
    if not paths:
        _show_message_box("RouteNinja", "No paths to display.")
        return
    try:
        if merged:
            fg = build_multi_path_graph(bv, paths, title=title)
        else:
            # Stitch each individual path into one graph, side-by-side isn't
            # supported by FlowGraph directly — users pick one path from the
            # sidebar and call show_paths(paths=[one_path], merged=True) to
            # see it isolated.
            fg = build_single_path_graph(bv, paths[0], title=title)
        _show_graph_report(title, fg)
    except Exception:
        logging.exception("Failed to display RouteNinja flow graph.")

"""RouteNinja core: call-graph projection and path enumeration.

No PySide / BN UI types in this module — the GUI wrapper consumes this API.
All paths returned are lists of int start addresses; callers resolve to
functions lazily via the BinaryView when they need display strings.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from binaryninja import BinaryView, Function


@dataclass
class CallGraph:
    """Directed call graph projection.

    forward[u]  = sorted list of unique callee start addrs reachable from u
    reverse[u]  = sorted list of unique caller start addrs for u
    call_sites  = {(caller, callee) -> sorted list of call-instruction addrs}

    Keyed entirely by int addresses so the BFS never touches RPyC/Binja state.
    """
    forward: Dict[int, List[int]] = field(default_factory=dict)
    reverse: Dict[int, List[int]] = field(default_factory=dict)
    call_sites: Dict[Tuple[int, int], List[int]] = field(default_factory=dict)
    node_count: int = 0
    edge_count: int = 0
    build_time_s: float = 0.0


@dataclass
class PathEdge:
    caller: int
    callee: int
    call_sites: List[int]         # addresses inside `caller` that call `callee`


@dataclass
class AnnotatedPath:
    """A path with call-site edges resolved."""
    nodes: List[int]              # function start addresses, src -> dst
    edges: List[PathEdge]         # len == len(nodes) - 1


@dataclass
class CallTree:
    """BFS-expansion DAG rooted at a single function.

    Functions are deduplicated (each appears once even if reachable via many
    routes); `depth[addr]` is the shortest-hop distance from the root.
    `edges` holds the parent→child relation actually traversed during the
    BFS (never includes cycles back onto earlier-discovered nodes).

    direction is either "callees" (root → what it calls) or "callers"
    (root → what calls it). Semantically the same BFS, just on forward vs
    reverse adjacency.
    """
    root: int
    direction: str                # "callees" or "callers"
    nodes: List[int]              # BFS order, root first
    depth: Dict[int, int]         # addr -> hops from root
    edges: List[Tuple[int, int]]  # (parent, child) — already deduped
    truncated: bool               # True if BFS hit max_depth or the cap


class RouteNinja:
    """Path-finding over a BinaryView call graph.

    Lifecycle:
        pt = RouteNinja()
        pt.set_binaryview(bv)        # triggers lazy graph build on first search
        pt.set_source(func_or_addr)
        pt.set_target(func_or_addr)
        paths = pt.find_paths()

    The call graph is cached. Call invalidate_graph() after adding new
    functions / editing types / anything that moves call edges.
    """

    DEFAULT_MAX_DEPTH = 8
    DEFAULT_MAX_PATHS = 32

    def __init__(self):
        self.bv: Optional[BinaryView] = None
        self._graph: Optional[CallGraph] = None
        self.source_addr: Optional[int] = None
        self.target_addr: Optional[int] = None
        self.max_depth: int = self.DEFAULT_MAX_DEPTH
        self.max_paths: int = self.DEFAULT_MAX_PATHS
        self.last_paths: List[AnnotatedPath] = []

    # ---- BV + graph lifecycle ------------------------------------------------

    def set_binaryview(self, bv: Optional[BinaryView]) -> None:
        if bv is self.bv:
            return
        self.bv = bv
        self._graph = None
        self.source_addr = None
        self.target_addr = None
        self.last_paths = []

    def invalidate_graph(self) -> None:
        self._graph = None

    @property
    def graph(self) -> Optional[CallGraph]:
        return self._graph

    def ensure_graph(self) -> CallGraph:
        if self._graph is None:
            self._graph = self._build_call_graph()
        return self._graph

    def _build_call_graph(self) -> CallGraph:
        """Walk every function once, projecting callees and call sites.

        This is the most expensive operation we do; everything downstream is
        a pure-Python search over the projected dicts.
        """
        if self.bv is None:
            raise RuntimeError("RouteNinja has no BinaryView set.")

        t0 = time.time()
        forward: Dict[int, List[int]] = {}
        call_sites: Dict[Tuple[int, int], List[int]] = {}
        edge_count = 0

        for f in self.bv.functions:
            start = int(f.start)
            dedup_callees: Set[int] = set()
            # call_sites returns ReferenceSource objects (address + function).
            # Pair each call site with the function whose start that callsite
            # transfers to, when resolvable.
            per_callee_sites: Dict[int, List[int]] = {}
            try:
                for site in f.call_sites:
                    site_addr = int(site.address)
                    # Prefer the explicit callees list for resolution — indirect
                    # callsites without a known target simply have no entry.
                    # For each direct outgoing edge on this site we look at
                    # the addresses in get_callees.
                    for callee_addr in self.bv.get_callees(site_addr):
                        callee_addr = int(callee_addr)
                        dedup_callees.add(callee_addr)
                        per_callee_sites.setdefault(callee_addr, []).append(site_addr)
            except Exception:
                logging.exception(f"Failed to collect call sites for {f.name} @ {start:#x}")

            # Fallback / supplement: f.callees gives function-level resolution,
            # even when call sites collapse or indirect targets are known.
            try:
                for c in f.callees:
                    addr = int(c.start)
                    dedup_callees.add(addr)
                    per_callee_sites.setdefault(addr, [])
            except Exception:
                logging.exception(f"Failed to read callees for {f.name} @ {start:#x}")

            forward[start] = sorted(dedup_callees)
            edge_count += len(dedup_callees)
            for callee_addr, sites in per_callee_sites.items():
                key = (start, callee_addr)
                call_sites[key] = sorted(set(sites))

        # Build reverse adjacency in one pass.
        reverse: Dict[int, List[int]] = {u: [] for u in forward}
        for u, vs in forward.items():
            for v in vs:
                reverse.setdefault(v, []).append(u)
        for u in list(reverse):
            reverse[u] = sorted(set(reverse[u]))

        graph = CallGraph(
            forward=forward,
            reverse=reverse,
            call_sites=call_sites,
            node_count=len(forward),
            edge_count=edge_count,
            build_time_s=time.time() - t0,
        )
        logging.debug(
            f"RouteNinja call graph built: {graph.node_count} nodes, "
            f"{graph.edge_count} edges, {graph.build_time_s*1000:.0f}ms"
        )
        return graph

    # ---- Endpoint setters ----------------------------------------------------

    def _coerce_address(self, fn_or_addr) -> Optional[int]:
        if fn_or_addr is None:
            return None
        if isinstance(fn_or_addr, int):
            return fn_or_addr
        start = getattr(fn_or_addr, "start", None)
        if isinstance(start, int):
            return start
        try:
            return int(fn_or_addr)
        except (TypeError, ValueError):
            return None

    def set_source(self, fn_or_addr) -> Optional[int]:
        addr = self._coerce_address(fn_or_addr)
        self.source_addr = addr
        return addr

    def set_target(self, fn_or_addr) -> Optional[int]:
        addr = self._coerce_address(fn_or_addr)
        self.target_addr = addr
        return addr

    def swap_endpoints(self) -> None:
        self.source_addr, self.target_addr = self.target_addr, self.source_addr

    # ---- Search --------------------------------------------------------------

    def find_paths(
        self,
        src: Optional[int] = None,
        dst: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_paths: Optional[int] = None,
    ) -> List[AnnotatedPath]:
        src = src if src is not None else self.source_addr
        dst = dst if dst is not None else self.target_addr
        if src is None or dst is None:
            raise ValueError("Both source and target must be set before searching.")
        depth = int(max_depth if max_depth is not None else self.max_depth)
        cap = int(max_paths if max_paths is not None else self.max_paths)

        graph = self.ensure_graph()
        raw_paths = _bidir_shortest_paths(graph.forward, graph.reverse, src, dst, depth, cap)
        annotated = [self._annotate(p, graph) for p in raw_paths]
        self.last_paths = annotated
        return annotated

    def find_all_callers(self, target: Optional[int] = None,
                         max_depth: Optional[int] = None,
                         max_callers: int = 200) -> List[int]:
        """BFS backward from the target. Useful when the user hasn't fixed a
        source yet — returns every function that can reach target within
        max_depth hops."""
        target = target if target is not None else self.target_addr
        if target is None:
            raise ValueError("Target must be set.")
        depth = int(max_depth if max_depth is not None else self.max_depth)
        graph = self.ensure_graph()
        seen: Set[int] = {target}
        frontier = [target]
        for _ in range(depth):
            nxt = []
            for u in frontier:
                for v in graph.reverse.get(u, ()):
                    if v not in seen:
                        seen.add(v)
                        nxt.append(v)
                        if len(seen) > max_callers:
                            return sorted(seen - {target})
            if not nxt:
                break
            frontier = nxt
        return sorted(seen - {target})

    def build_call_tree(
        self,
        root: Optional[int] = None,
        direction: str = "callees",
        max_depth: Optional[int] = None,
        max_nodes: int = 500,
    ) -> CallTree:
        """BFS expansion from a single function.

        direction="callees" → root is at the top, each child is a function
            the parent calls (directly or indirectly). Answers "what does
            this code reach?"
        direction="callers" → root is at the top, each child is a function
            that calls the parent. Answers "who can reach this code?"

        Nodes are deduplicated: a function reachable via multiple routes
        appears once, at its shortest hop distance. Cycles are therefore
        implicit — any back-edge into an earlier-discovered node is skipped.
        `max_nodes` caps the output; trees larger than that are truncated
        at the current BFS frontier (marked via `truncated=True`).
        """
        if direction not in ("callees", "callers"):
            raise ValueError(f"direction must be 'callees' or 'callers', got {direction!r}")
        root = root if root is not None else self.source_addr
        if root is None:
            raise ValueError("Root function must be provided (or set a source).")
        depth_cap = int(max_depth if max_depth is not None else self.max_depth)

        graph = self.ensure_graph()
        adj = graph.forward if direction == "callees" else graph.reverse

        nodes: List[int] = [root]
        depth_of: Dict[int, int] = {root: 0}
        edges: List[Tuple[int, int]] = []
        frontier: List[int] = [root]
        truncated = False

        for d in range(depth_cap):
            if not frontier:
                break
            nxt: List[int] = []
            for u in frontier:
                for v in adj.get(u, ()):
                    if v in depth_of:
                        # Still emit the edge when it's a back-edge to an
                        # already-discovered node at the same or earlier
                        # depth. Graph view dedupes edges, so this is safe
                        # and preserves the call relationship on display.
                        edges.append((u, v))
                        continue
                    depth_of[v] = d + 1
                    nodes.append(v)
                    edges.append((u, v))
                    nxt.append(v)
                    if len(nodes) >= max_nodes:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
            frontier = nxt

        # If we stopped because of depth_cap and the last frontier still had
        # unexplored children in the graph, flag as truncated.
        if not truncated and frontier:
            for u in frontier:
                if any(v not in depth_of for v in adj.get(u, ())):
                    truncated = True
                    break

        return CallTree(
            root=root,
            direction=direction,
            nodes=nodes,
            depth=depth_of,
            edges=edges,
            truncated=truncated,
        )

    def _annotate(self, path: Sequence[int], graph: CallGraph) -> AnnotatedPath:
        nodes = list(path)
        edges: List[PathEdge] = []
        for i in range(len(nodes) - 1):
            u, v = nodes[i], nodes[i + 1]
            sites = graph.call_sites.get((u, v), [])
            edges.append(PathEdge(caller=u, callee=v, call_sites=list(sites)))
        return AnnotatedPath(nodes=nodes, edges=edges)

    # ---- Name helpers --------------------------------------------------------

    def function_name(self, addr: int) -> str:
        """Best-effort name resolution.

        Order: named function > symbol at address (covers imports like
        ImportAddressSymbol for RegCreateKeyExW etc.) > hex.
        """
        if self.bv is None:
            return hex(addr)
        try:
            fns = self.bv.get_functions_at(addr)
            if fns:
                return str(fns[0].name)
        except Exception:
            pass
        try:
            sym = self.bv.get_symbol_at(addr)
            if sym is not None:
                name = getattr(sym, "full_name", None) or getattr(sym, "name", None)
                if name:
                    return str(name)
        except Exception:
            pass
        return hex(addr)

    def format_path(self, path: AnnotatedPath) -> str:
        return " -> ".join(self.function_name(n) for n in path.nodes)


# ---- BFS / search primitives (module-level, no BV dependency) --------------

def _level_bfs(graph: Dict[int, List[int]], start: int, max_depth: int
               ) -> Tuple[Dict[int, int], Dict[int, List[int]]]:
    dist: Dict[int, int] = {start: 0}
    pred: Dict[int, List[int]] = {start: []}
    frontier: List[int] = [start]
    for d in range(max_depth):
        nxt: List[int] = []
        for u in frontier:
            for v in graph.get(u, ()):
                if v not in dist:
                    dist[v] = d + 1
                    pred[v] = [u]
                    nxt.append(v)
                elif dist[v] == d + 1:
                    pred[v].append(u)
        if not nxt:
            break
        frontier = nxt
    return dist, pred


def _enumerate_to_root(pred: Dict[int, List[int]], node: int, limit: int
                       ) -> List[List[int]]:
    """DFS via predecessor links (iterative). Root-first paths terminating at `node`."""
    out: List[List[int]] = []
    # Stack entries: (current, path_from_node_back_to_current)
    stack: List[Tuple[int, List[int]]] = [(node, [node])]
    while stack and len(out) < limit:
        cur, path = stack.pop()
        parents = pred.get(cur, [])
        if not parents:
            out.append(list(reversed(path)))
            continue
        for p in parents:
            if p in path:          # cycle guard — level BFS already prevents this
                continue            # defensively skip anyway
            stack.append((p, path + [p]))
    return out


def _bidir_shortest_paths(
    forward: Dict[int, List[int]],
    reverse: Dict[int, List[int]],
    src: int,
    dst: int,
    max_depth: int,
    max_paths: int,
) -> List[List[int]]:
    if src == dst:
        return [[src]]
    if src not in forward and src not in reverse:
        return []
    if dst not in forward and dst not in reverse:
        return []

    half = max_depth // 2
    other = max_depth - half
    df, pf = _level_bfs(forward, src, half)
    dr, pr = _level_bfs(reverse, dst, other)

    meet = set(df) & set(dr)
    if not meet:
        return []

    # Order meeting nodes by total path length to keep output shortest-first.
    ordered = sorted(meet, key=lambda m: df[m] + dr[m])

    out: List[List[int]] = []
    seen: Set[Tuple[int, ...]] = set()
    for m in ordered:
        if len(out) >= max_paths:
            break
        left = _enumerate_to_root(pf, m, max_paths * 4)
        right = _enumerate_to_root(pr, m, max_paths * 4)
        for L in left:
            if len(out) >= max_paths:
                break
            for R in right:
                if len(out) >= max_paths:
                    break
                full = L + list(reversed(R))[1:]
                if len(full) - 1 > max_depth:
                    continue
                if len(set(full)) != len(full):  # no revisits
                    continue
                key = tuple(full)
                if key in seen:
                    continue
                seen.add(key)
                out.append(full)

    out.sort(key=len)
    return out

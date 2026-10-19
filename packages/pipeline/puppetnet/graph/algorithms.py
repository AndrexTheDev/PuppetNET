"""Pure-Python graph algorithms used by the maintenance layer.

Kept in the package (instead of the ``graph_analytics.py`` script) because two
callers need them: the maintenance CLI and the Cypher analytics writer. Neither
Neo4j GDS nor NetworkX is a dependency — AuraDB Free does not ship GDS, and a
free-tier deployment must not need a paid plugin to answer "who holds this
network together?".

Everything here is deterministic: node order is sorted before iteration, so a
run over the same graph produces the same numbers on every machine.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping, Sequence

__all__ = [
    "brandes_betweenness",
    "strongly_connected_components",
    "shortest_cycle_through",
]


def brandes_betweenness(
    nodes: Sequence[str],
    adjacency: Mapping[str, Sequence[tuple[str, float]]],
    *,
    deadline: float | None = None,
) -> dict[str, float]:
    """Brandes' unweighted betweenness centrality, O(V·E).

    GDS is not available on AuraDB Free, so the projection is scored here. The
    graph is treated as undirected (an ownership tie connects two actors
    whichever way it was written) and the result is normalised by the number of
    distinct node pairs, which puts every score in [0, 1] and makes it directly
    comparable with the other anomaly components.

    Deliberately unweighted: betweenness on a weighted graph needs Dijkstra per
    source, and on a 20 000-node projection that is minutes of CPU on a GitHub
    runner for a ranking that rarely changes. Edge *confidence* is applied
    earlier, when the projection is thresholded.
    """
    betweenness = dict.fromkeys(nodes, 0.0)
    total = len(nodes)
    if total < 3:
        return betweenness

    visited = 0
    for source in nodes:
        if deadline is not None and visited and visited % 128 == 0 and time.perf_counter() > deadline:
            break
        visited += 1
        stack: list[str] = []
        predecessors: dict[str, list[str]] = {node: [] for node in nodes}
        sigma = dict.fromkeys(nodes, 0.0)
        sigma[source] = 1.0
        distance = dict.fromkeys(nodes, -1)
        distance[source] = 0

        queue = deque([source])
        while queue:
            current = queue.popleft()
            stack.append(current)
            for neighbour, _weight in adjacency.get(current, ()):  # noqa: B007
                if neighbour not in distance:
                    continue
                if distance[neighbour] < 0:
                    distance[neighbour] = distance[current] + 1
                    queue.append(neighbour)
                if distance[neighbour] == distance[current] + 1:
                    sigma[neighbour] += sigma[current]
                    predecessors[neighbour].append(current)

        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            current = stack.pop()
            for predecessor in predecessors[current]:
                if sigma[current] <= 0:
                    continue
                delta[predecessor] += (sigma[predecessor] / sigma[current]) * (1 + delta[current])
            if current != source:
                betweenness[current] += delta[current]

    # Undirected graphs count every pair twice. A partial run (deadline hit)
    # divided by fewer sources, so it is rescaled by the fraction visited to stay
    # comparable with a complete one.
    norm = (total - 1) * (total - 2)
    if norm <= 0:
        return betweenness
    scale = total / visited if visited else 1.0
    return {node: min(1.0, value * scale / norm) for node, value in betweenness.items()}


def strongly_connected_components(
    nodes: Sequence[str],
    adjacency: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    """Tarjan's SCCs, iteratively.

    A circular ownership structure is, precisely, a strongly connected
    component with more than one member (or a self-loop). Finding SCCs is both
    exact and linear; the alternative — ``MATCH p=(a)-[*..6]->(a)`` — enumerates
    every path of length ≤ 6, which on a 50 000-edge ownership graph is millions
    of paths and would not survive AuraDB Free's memory budget.

    Nodes are visited in sorted order and each component is returned sorted, so
    the output is stable across runs.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in sorted(nodes):
        if root in index_of:
            continue
        # (node, iterator over its successors) — an explicit stack instead of
        # recursion, because a 20 000-node chain would blow the Python limit.
        work: list[tuple[str, list[str], int]] = [(root, sorted(set(adjacency.get(root, ()))), 0)]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, successors, position = work[-1]
            if position < len(successors):
                work[-1] = (node, successors, position + 1)
                successor = successors[position]
                if successor not in index_of:
                    index_of[successor] = lowlink[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, sorted(set(adjacency.get(successor, ()))), 0))
                elif successor in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[successor])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))

    components.sort(key=lambda component: (-len(component), component[0]))
    return components


def shortest_cycle_through(
    component: Sequence[str],
    adjacency: Mapping[str, Sequence[str]],
    *,
    max_length: int = 12,
    start: str | None = None,
) -> list[str] | None:
    """A concrete cycle inside one SCC, as ``[start, ..., start]``.

    Used for the report: ``["a", "b", "c", "a"]`` is auditable by a human in a
    way that "these 9 nodes form an SCC" is not. BFS from the lexicographically
    smallest member (or ``start``) returns the shortest cycle through it.
    """
    members = set(component)
    if len(members) < 2 and not (component and component[0] in adjacency.get(component[0], ())):
        return None
    origin = start or min(members)
    if origin not in members:
        return None

    # Breadth-first search over members, tracking the path. A successor equal to
    # ``origin`` closes the cycle; anything longer than ``max_length`` is not
    # reported (the SCC is still reported in full).
    queue: deque[list[str]] = deque([[origin]])
    seen: set[str] = {origin}
    while queue:
        path = queue.popleft()
        if len(path) > max_length:
            continue
        for successor in sorted(set(adjacency.get(path[-1], ()))):
            if successor == origin and len(path) >= 2:
                return [*path, origin]
            if successor in members and successor not in seen:
                seen.add(successor)
                queue.append([*path, successor])
    return None

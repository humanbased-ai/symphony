"""
Depth-First Search (DFS) — two classic applications.

1. Graph connectivity detection  (undirected graph)
2. Maze path-finding              (2-D grid)

Each application ships both a recursive and an iterative (explicit stack)
implementation so the two styles can be compared side by side.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


# ---------------------------------------------------------------------------
# Application 1 — Graph connectivity
# ---------------------------------------------------------------------------

Graph = dict[str, list[str]]


def build_graph(edges: list[tuple[str, str]]) -> Graph:
    """Return an undirected adjacency-list graph from an edge list."""
    g: Graph = defaultdict(list)
    for u, v in edges:
        g[u].append(v)
        g[v].append(u)
    return g


def _dfs_visit_recursive(graph: Graph, node: str, visited: set[str]) -> None:
    visited.add(node)
    for neighbour in graph.get(node, []):
        if neighbour not in visited:
            _dfs_visit_recursive(graph, neighbour, visited)


def connected_components_recursive(graph: Graph) -> list[set[str]]:
    """Return all connected components using recursive DFS."""
    visited: set[str] = set()
    components: list[set[str]] = []
    for node in graph:
        if node not in visited:
            component: set[str] = set()
            _dfs_visit_recursive(graph, node, component)
            visited |= component
            components.append(component)
    return components


def connected_components_iterative(graph: Graph) -> list[set[str]]:
    """Return all connected components using iterative DFS (explicit stack)."""
    visited: set[str] = set()
    components: list[set[str]] = []
    for start in graph:
        if start in visited:
            continue
        component: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            for neighbour in graph.get(node, []):
                if neighbour not in component:
                    stack.append(neighbour)
        visited |= component
        components.append(component)
    return components


def is_connected(graph: Graph) -> bool:
    """Return True if the graph is fully connected (single component)."""
    if not graph:
        return True
    components = connected_components_iterative(graph)
    return len(components) == 1


# ---------------------------------------------------------------------------
# Application 2 — Maze path-finding
# ---------------------------------------------------------------------------

Maze = list[list[str]]
Point = tuple[int, int]

WALL = "#"
OPEN = "."
START = "S"
END = "E"

_DIRECTIONS: list[Point] = [(0, 1), (0, -1), (1, 0), (-1, 0)]


def _in_bounds(maze: Maze, r: int, c: int) -> bool:
    return 0 <= r < len(maze) and 0 <= c < len(maze[0])


def _passable(maze: Maze, r: int, c: int) -> bool:
    return _in_bounds(maze, r, c) and maze[r][c] != WALL


def _find(maze: Maze, char: str) -> Optional[Point]:
    for r, row in enumerate(maze):
        for c, cell in enumerate(row):
            if cell == char:
                return (r, c)
    return None


def maze_path_recursive(maze: Maze) -> Optional[list[Point]]:
    """
    Find a path from S to E using recursive DFS.
    Returns the path as a list of (row, col) coordinates, or None if no path.
    """
    start = _find(maze, START)
    end = _find(maze, END)
    if start is None or end is None:
        return None

    visited: set[Point] = set()

    def dfs(pos: Point, path: list[Point]) -> Optional[list[Point]]:
        if pos in visited:
            return None
        visited.add(pos)
        path = path + [pos]
        if pos == end:
            return path
        r, c = pos
        for dr, dc in _DIRECTIONS:
            nr, nc = r + dr, c + dc
            if _passable(maze, nr, nc) and (nr, nc) not in visited:
                result = dfs((nr, nc), path)
                if result is not None:
                    return result
        return None

    return dfs(start, [])


def maze_path_iterative(maze: Maze) -> Optional[list[Point]]:
    """
    Find a path from S to E using iterative DFS (explicit stack).
    Returns the path as a list of (row, col) coordinates, or None if no path.
    """
    start = _find(maze, START)
    end = _find(maze, END)
    if start is None or end is None:
        return None

    # Stack entries: (current_position, path_so_far)
    stack: list[tuple[Point, list[Point]]] = [(start, [start])]
    visited: set[Point] = set()

    while stack:
        pos, path = stack.pop()
        if pos in visited:
            continue
        visited.add(pos)
        if pos == end:
            return path
        r, c = pos
        for dr, dc in _DIRECTIONS:
            nr, nc = r + dr, c + dc
            if _passable(maze, nr, nc) and (nr, nc) not in visited:
                stack.append(((nr, nc), path + [(nr, nc)]))

    return None


# ---------------------------------------------------------------------------
# Quick demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- Graph connectivity ---
    edges = [("A", "B"), ("B", "C"), ("D", "E")]
    g = build_graph(edges)
    components = connected_components_iterative(g)
    print("Graph edges:", edges)
    print("Connected:", is_connected(g))
    print("Components:", [sorted(c) for c in components])
    print()

    # --- Maze ---
    raw_maze = [
        "S.#..",
        "..#..",
        "...#.",
        "....E",
    ]
    maze: Maze = [list(row) for row in raw_maze]
    path = maze_path_iterative(maze)
    print("Maze:")
    for row in raw_maze:
        print(" ", row)
    print("Path found (iterative):", path)

    path_rec = maze_path_recursive(maze)
    print("Path found (recursive):", path_rec)

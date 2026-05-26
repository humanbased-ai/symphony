"""Tests for DFS graph connectivity and maze path-finding."""

import pytest

from examples.dfs_algorithms import (
    build_graph,
    connected_components_iterative,
    connected_components_recursive,
    is_connected,
    maze_path_iterative,
    maze_path_recursive,
)


# ---------------------------------------------------------------------------
# Graph connectivity
# ---------------------------------------------------------------------------


def _sorted_components(components):
    return sorted(sorted(c) for c in components)


class TestGraphConnectivity:
    def test_single_component(self):
        g = build_graph([("A", "B"), ("B", "C"), ("C", "A")])
        assert is_connected(g)

    def test_two_components(self):
        g = build_graph([("A", "B"), ("C", "D")])
        assert not is_connected(g)
        components = connected_components_iterative(g)
        assert _sorted_components(components) == [["A", "B"], ["C", "D"]]

    def test_isolated_nodes(self):
        # Build a graph with explicit isolated nodes
        g = build_graph([("A", "B")])
        g["C"]  # ensure C appears as key with empty neighbours
        components = connected_components_iterative(g)
        assert len(components) == 2

    def test_empty_graph(self):
        g = build_graph([])
        assert is_connected(g)

    def test_single_node(self):
        g = build_graph([])
        g["X"] = []
        components = connected_components_iterative(g)
        assert len(components) == 1

    def test_recursive_matches_iterative(self):
        edges = [("1", "2"), ("2", "3"), ("4", "5"), ("6", "7"), ("5", "6")]
        g = build_graph(edges)
        rec = _sorted_components(connected_components_recursive(g))
        itr = _sorted_components(connected_components_iterative(g))
        assert rec == itr

    def test_chain_graph(self):
        edges = [(str(i), str(i + 1)) for i in range(9)]
        g = build_graph(edges)
        assert is_connected(g)

    def test_three_isolated_nodes(self):
        g: dict = {"A": [], "B": [], "C": []}
        components = connected_components_iterative(g)
        assert len(components) == 3


# ---------------------------------------------------------------------------
# Maze path-finding
# ---------------------------------------------------------------------------

SIMPLE_MAZE = [
    list("S..."),
    list("####"),
    list("...."),
    list("...E"),
]

SOLVABLE_MAZE = [
    list("S.#.."),
    list("..#.."),
    list("...#."),
    list("....E"),
]

UNSOLVABLE_MAZE = [
    list("S###"),
    list("####"),
    list("###E"),
]

OPEN_MAZE = [
    list("S.."),
    list("..."),
    list("..E"),
]


class TestMazePathFinding:
    def test_solvable_iterative(self):
        path = maze_path_iterative(SOLVABLE_MAZE)
        assert path is not None
        assert path[0] == (0, 0)   # starts at S
        assert path[-1] == (3, 4)  # ends at E

    def test_solvable_recursive(self):
        path = maze_path_recursive(SOLVABLE_MAZE)
        assert path is not None
        assert path[0] == (0, 0)
        assert path[-1] == (3, 4)

    def test_unsolvable_returns_none_iterative(self):
        assert maze_path_iterative(UNSOLVABLE_MAZE) is None

    def test_unsolvable_returns_none_recursive(self):
        assert maze_path_recursive(UNSOLVABLE_MAZE) is None

    def test_path_is_contiguous(self):
        path = maze_path_iterative(SOLVABLE_MAZE)
        assert path is not None
        for (r1, c1), (r2, c2) in zip(path, path[1:]):
            assert abs(r1 - r2) + abs(c1 - c2) == 1

    def test_open_maze_both_methods_agree(self):
        itr = maze_path_iterative(OPEN_MAZE)
        rec = maze_path_recursive(OPEN_MAZE)
        assert itr is not None
        assert rec is not None
        assert itr[0] == rec[0] == (0, 0)
        assert itr[-1] == rec[-1] == (2, 2)

    def test_no_start_returns_none(self):
        maze = [list("...."), list("...E")]
        assert maze_path_iterative(maze) is None
        assert maze_path_recursive(maze) is None

    def test_no_end_returns_none(self):
        maze = [list("S..."), list("....")]
        assert maze_path_iterative(maze) is None
        assert maze_path_recursive(maze) is None

    def test_start_equals_end_not_supported(self):
        # When S and E share the same cell this won't happen in valid input;
        # a maze with only S (no E) should return None.
        maze = [list("S...")]
        assert maze_path_iterative(maze) is None

    def test_simple_blocked_maze(self):
        # SIMPLE_MAZE has a full wall row so it is unsolvable.
        assert maze_path_iterative(SIMPLE_MAZE) is None
        assert maze_path_recursive(SIMPLE_MAZE) is None

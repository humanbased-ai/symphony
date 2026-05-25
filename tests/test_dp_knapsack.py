import pytest
from symphony.algorithms.dp_knapsack import knapsack


def test_basic():
    # capacity=50, classic textbook example
    weights = [10, 20, 30]
    values = [60, 100, 120]
    assert knapsack(50, weights, values) == 220


def test_empty_items():
    assert knapsack(10, [], []) == 0


def test_zero_capacity():
    assert knapsack(0, [1, 2, 3], [10, 20, 30]) == 0


def test_single_item_fits():
    assert knapsack(5, [5], [42]) == 42


def test_single_item_does_not_fit():
    assert knapsack(4, [5], [42]) == 0


def test_all_items_fit():
    weights = [1, 2, 3]
    values = [10, 20, 30]
    assert knapsack(10, weights, values) == 60


def test_must_choose_subset():
    # Only one of items 0 or 1 can fit; item 1 has higher value
    weights = [3, 4]
    values = [5, 7]
    assert knapsack(4, weights, values) == 7


def test_exact_capacity_fill():
    weights = [2, 3, 5]
    values = [3, 4, 8]
    # Best: take items with w=2 and w=5 → value 11
    assert knapsack(7, weights, values) == 11

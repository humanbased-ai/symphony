"""Tests for examples/quicksort.py."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from examples.quicksort import quicksort


def test_empty_list():
    assert quicksort([]) == []


def test_single_element():
    assert quicksort([42]) == [42]


def test_already_sorted():
    assert quicksort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert quicksort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_duplicates():
    assert quicksort([3, 1, 4, 1, 5, 9, 2, 6, 5]) == sorted([3, 1, 4, 1, 5, 9, 2, 6, 5])


def test_all_equal():
    assert quicksort([7, 7, 7]) == [7, 7, 7]


def test_negative_numbers():
    assert quicksort([-3, 0, -1, 2, -5]) == [-5, -3, -1, 0, 2]


def test_strings():
    assert quicksort(["banana", "apple", "cherry"]) == ["apple", "banana", "cherry"]


def test_does_not_mutate_input():
    original = [3, 1, 2]
    quicksort(original)
    assert original == [3, 1, 2]

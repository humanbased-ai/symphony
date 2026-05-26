"""Tests for the recursive quicksort example."""

import pytest

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from examples.quicksort import quicksort


def test_empty_list():
    assert quicksort([]) == []


def test_single_element():
    assert quicksort([7]) == [7]


def test_already_sorted():
    assert quicksort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert quicksort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_mixed():
    assert quicksort([3, 6, 8, 10, 1, 2, 1]) == [1, 1, 2, 3, 6, 8, 10]


def test_duplicates():
    assert quicksort([4, 4, 4]) == [4, 4, 4]


def test_negative_numbers():
    assert quicksort([-3, 1, -1, 0, 2]) == [-3, -1, 0, 1, 2]


def test_original_list_unchanged():
    original = [3, 1, 2]
    quicksort(original)
    assert original == [3, 1, 2]

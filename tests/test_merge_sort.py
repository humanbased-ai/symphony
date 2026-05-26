import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from merge_sort import merge_sort


def test_basic_unsorted():
    assert merge_sort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_already_sorted():
    assert merge_sort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert merge_sort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_single_element():
    assert merge_sort([42]) == [42]


def test_empty():
    assert merge_sort([]) == []


def test_duplicates():
    assert merge_sort([2, 2, 2, 1, 1]) == [1, 1, 2, 2, 2]


def test_negative_numbers():
    assert merge_sort([-3, 0, -1, 2]) == [-3, -1, 0, 2]


def test_input_not_mutated():
    original = [3, 1, 2]
    merge_sort(original)
    assert original == [3, 1, 2]

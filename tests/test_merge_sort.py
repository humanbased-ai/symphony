"""Tests for the merge sort example."""

import pytest

from examples.merge_sort import merge_sort


def test_average_case():
    assert merge_sort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_already_sorted():
    assert merge_sort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert merge_sort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_single_element():
    assert merge_sort([42]) == [42]


def test_empty_list():
    assert merge_sort([]) == []


def test_does_not_mutate_input():
    original = [3, 1, 2]
    merge_sort(original)
    assert original == [3, 1, 2]

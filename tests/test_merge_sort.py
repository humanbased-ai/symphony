import pytest
from symphony.merge_sort import merge, merge_sort


def test_merge_sort_basic():
    assert merge_sort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_merge_sort_empty():
    assert merge_sort([]) == []


def test_merge_sort_single():
    assert merge_sort([42]) == [42]


def test_merge_sort_already_sorted():
    assert merge_sort([1, 2, 3]) == [1, 2, 3]


def test_merge_sort_reverse():
    assert merge_sort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_merge_sort_duplicates():
    assert merge_sort([2, 2, 2]) == [2, 2, 2]


def test_merge_sort_does_not_mutate():
    arr = [3, 1, 2]
    merge_sort(arr)
    assert arr == [3, 1, 2]


def test_merge_basic():
    assert merge([1, 3, 5], [2, 4, 6]) == [1, 2, 3, 4, 5, 6]


def test_merge_empty_left():
    assert merge([], [1, 2]) == [1, 2]


def test_merge_empty_right():
    assert merge([1, 2], []) == [1, 2]

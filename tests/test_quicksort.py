import pytest
from symphony.examples.quicksort import quicksort


def test_empty_list():
    assert quicksort([]) == []


def test_single_element():
    assert quicksort([42]) == [42]


def test_already_sorted():
    assert quicksort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_sorted():
    assert quicksort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_unsorted():
    assert quicksort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_duplicates():
    assert quicksort([2, 2, 2]) == [2, 2, 2]


def test_negative_numbers():
    assert quicksort([-3, 1, -1, 2, 0]) == [-3, -1, 0, 1, 2]


def test_single_duplicate_pair():
    assert quicksort([2, 1, 2]) == [1, 2, 2]

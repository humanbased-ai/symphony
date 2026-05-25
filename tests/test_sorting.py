from symphony.sorting import bubble_sort


def test_basic():
    assert bubble_sort([5, 3, 1, 4, 2]) == [1, 2, 3, 4, 5]


def test_already_sorted():
    assert bubble_sort([1, 2, 3]) == [1, 2, 3]


def test_reverse_sorted():
    assert bubble_sort([3, 2, 1]) == [1, 2, 3]


def test_single_element():
    assert bubble_sort([42]) == [42]


def test_empty():
    assert bubble_sort([]) == []


def test_duplicates():
    assert bubble_sort([3, 1, 2, 1]) == [1, 1, 2, 3]

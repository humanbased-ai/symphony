import pytest
from symphony.binary_search import binary_search, binary_search_recursive


@pytest.mark.parametrize("fn", [binary_search, binary_search_recursive])
class TestBinarySearch:
    def test_found_first(self, fn):
        assert fn([1, 3, 5, 7, 9], 1) == 0

    def test_found_last(self, fn):
        assert fn([1, 3, 5, 7, 9], 9) == 4

    def test_found_middle(self, fn):
        assert fn([1, 3, 5, 7, 9], 5) == 2

    def test_not_found(self, fn):
        assert fn([1, 3, 5, 7, 9], 4) == -1

    def test_empty_list(self, fn):
        assert fn([], 1) == -1

    def test_single_element_found(self, fn):
        assert fn([42], 42) == 0

    def test_single_element_not_found(self, fn):
        assert fn([42], 7) == -1

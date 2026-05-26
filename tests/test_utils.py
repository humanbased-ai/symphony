import unittest

from symphony.utils import merge_sort, merge_sorted_lists


class MergeSortedListsTests(unittest.TestCase):
    def test_both_empty(self):
        self.assertEqual([], merge_sorted_lists([], []))

    def test_first_empty(self):
        self.assertEqual([1, 2, 3], merge_sorted_lists([], [1, 2, 3]))

    def test_second_empty(self):
        self.assertEqual([1, 2, 3], merge_sorted_lists([1, 2, 3], []))

    def test_single_element_lists(self):
        self.assertEqual([1, 2], merge_sorted_lists([1], [2]))

    def test_single_element_reversed(self):
        self.assertEqual([1, 2], merge_sorted_lists([2], [1]))

    def test_already_sorted_interleaved(self):
        self.assertEqual([1, 2, 3, 4, 5, 6], merge_sorted_lists([1, 3, 5], [2, 4, 6]))

    def test_duplicate_values(self):
        self.assertEqual([1, 1, 2, 2, 3, 3], merge_sorted_lists([1, 2, 3], [1, 2, 3]))

    def test_all_duplicates(self):
        self.assertEqual([5, 5, 5, 5], merge_sorted_lists([5, 5], [5, 5]))

    def test_input_not_mutated(self):
        a = [1, 3]
        b = [2, 4]
        merge_sorted_lists(a, b)
        self.assertEqual([1, 3], a)
        self.assertEqual([2, 4], b)


class MergeSortTests(unittest.TestCase):
    def _identity(self, x):
        return x

    def test_empty_list(self):
        self.assertEqual([], merge_sort([], key=self._identity))

    def test_single_element(self):
        self.assertEqual([42], merge_sort([42], key=self._identity))

    def test_already_sorted(self):
        self.assertEqual([1, 2, 3, 4, 5], merge_sort([1, 2, 3, 4, 5], key=self._identity))

    def test_reverse_sorted(self):
        self.assertEqual([1, 2, 3, 4, 5], merge_sort([5, 4, 3, 2, 1], key=self._identity))

    def test_duplicate_values(self):
        self.assertEqual([1, 1, 2, 2, 3], merge_sort([2, 1, 3, 1, 2], key=self._identity))

    def test_all_duplicates(self):
        self.assertEqual([7, 7, 7], merge_sort([7, 7, 7], key=self._identity))

    def test_custom_key_sort_by_string_length(self):
        words = ["banana", "fig", "apple", "kiwi", "plum"]
        result = merge_sort(words, key=len)
        self.assertEqual(["fig", "kiwi", "plum", "apple", "banana"], result)

    def test_custom_key_sort_by_second_element(self):
        pairs = [(3, "c"), (1, "a"), (2, "b")]
        result = merge_sort(pairs, key=lambda p: p[1])
        self.assertEqual([(1, "a"), (2, "b"), (3, "c")], result)

    def test_custom_key_sort_by_negative(self):
        result = merge_sort([1, 3, 2, 5, 4], key=lambda x: -x)
        self.assertEqual([5, 4, 3, 2, 1], result)

    def test_stable_sort_equal_keys(self):
        # Items with equal keys must preserve their original relative order.
        items = [("b", 1), ("a", 1), ("c", 2)]
        result = merge_sort(items, key=lambda x: x[1])
        self.assertEqual([("b", 1), ("a", 1), ("c", 2)], result)

    def test_input_not_mutated(self):
        original = [3, 1, 2]
        merge_sort(original, key=self._identity)
        self.assertEqual([3, 1, 2], original)


if __name__ == "__main__":
    unittest.main()

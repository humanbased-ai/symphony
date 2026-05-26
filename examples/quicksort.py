"""Quicksort — a classic divide-and-conquer recursive sorting algorithm.

Structure:
  Base case:  a list of 0 or 1 elements is already sorted; return it as-is.
  Recursive:  choose a pivot, partition the remaining elements into two
              sub-lists (≤ pivot and > pivot), sort each sub-list
              recursively, then concatenate: sorted_left + [pivot] + sorted_right.

Time complexity:  O(n log n) average, O(n²) worst case (already-sorted input
                  with naive pivot choice).
Space complexity: O(n) auxiliary (new lists at each level of recursion).
"""

from typing import TypeVar, Sequence

T = TypeVar("T")


def quicksort(items: list) -> list:
    """Return a new sorted list using quicksort.

    Args:
        items: Any list whose elements support the < operator.

    Returns:
        A new list containing the same elements in ascending order.
    """
    # Base case: zero or one element — nothing to sort.
    if len(items) <= 1:
        return list(items)

    # Pick the middle element as pivot to avoid O(n²) on sorted inputs.
    mid = len(items) // 2
    pivot = items[mid]

    # Partition into three groups; equal elements collect in the middle.
    less = [x for x in items if x < pivot]
    equal = [x for x in items if x == pivot]
    greater = [x for x in items if x > pivot]

    # Recursive step: sort each partition, then concatenate.
    return quicksort(less) + equal + quicksort(greater)

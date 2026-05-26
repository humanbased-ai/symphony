"""
Recursive Quicksort Example

Quicksort is a divide-and-conquer algorithm that works by selecting a pivot
element and partitioning the array into two sub-arrays: elements less than
the pivot and elements greater than the pivot. It then recursively sorts each
sub-array.

Time complexity: O(n log n) average, O(n^2) worst case
Space complexity: O(log n) average for the call stack
"""


def quicksort(arr: list) -> list:
    # Base case: arrays of 0 or 1 elements are already sorted
    if len(arr) <= 1:
        return arr

    # Choose the middle element as pivot to avoid worst-case on sorted input
    pivot = arr[len(arr) // 2]

    # Partition into three groups
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    # Recursively sort left and right partitions, then combine
    return quicksort(left) + middle + quicksort(right)

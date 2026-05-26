"""
Quicksort — a classic divide-and-conquer recursive sorting algorithm.

Time complexity:  O(n log n) average, O(n²) worst case
Space complexity: O(log n) average call-stack depth
"""


def quicksort(arr: list) -> list:
    """Return a new sorted list using recursive quicksort.

    Base case: a list of 0 or 1 elements is already sorted.
    Recursive case: pick a pivot, partition the remaining elements into
    items less-than and greater-than-or-equal-to the pivot, then
    recursively sort each partition and concatenate the results.
    """
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    less = [x for x in arr if x < pivot]
    equal = [x for x in arr if x == pivot]
    greater = [x for x in arr if x > pivot]

    return quicksort(less) + equal + quicksort(greater)


if __name__ == "__main__":
    samples = [
        [3, 6, 8, 10, 1, 2, 1],
        [],
        [42],
        [5, 4, 3, 2, 1],
        [1, 2, 3, 4, 5],
    ]
    for s in samples:
        print(f"{s} -> {quicksort(s)}")

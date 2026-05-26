"""
Merge Sort — divide-and-conquer sorting algorithm.

Time complexity:  O(n log n) in all cases.
Space complexity: O(n) auxiliary space for the temporary arrays used during merging.

Why O(n log n)?
  - DIVIDE: Each recursive call splits the list in half.
    Starting from n elements, we can halve at most log₂(n) times before
    reaching single-element lists.  That gives us log n levels of recursion.
  - CONQUER (merge): At each level of recursion we merge ALL the sub-lists back
    together.  The total work across all merges at one level is O(n) because
    every element is visited exactly once per level.
  - Combined: log n levels × O(n) work per level = O(n log n) total.

This is optimal for comparison-based sorting — no comparison sort can do
better than O(n log n) in the worst case (information-theoretic lower bound).
"""


def merge_sort(arr: list) -> list:
    """Return a new sorted list; the input list is not modified."""

    # ------------------------------------------------------------------ #
    # BASE CASE — a list with 0 or 1 element is already sorted.           #
    # Recursion unwinds from here back up to the original call.           #
    # ------------------------------------------------------------------ #
    if len(arr) <= 1:
        return arr

    # ------------------------------------------------------------------ #
    # DIVIDE — split the list into two roughly equal halves.              #
    #                                                                      #
    # Integer division gives the midpoint index.  For example:           #
    #   [3, 1, 4, 1, 5]  →  mid = 2                                      #
    #   left  = [3, 1]       (indices 0..mid-1)                           #
    #   right = [4, 1, 5]    (indices mid..end)                           #
    #                                                                      #
    # Each half is sorted independently by recursing into merge_sort.     #
    # This halving happens log₂(n) times before the base case is reached. #
    # ------------------------------------------------------------------ #
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])   # recursively sort the left half
    right = merge_sort(arr[mid:])  # recursively sort the right half

    # ------------------------------------------------------------------ #
    # MERGE — combine two sorted halves into one sorted list.             #
    #                                                                      #
    # We walk both halves simultaneously with two pointers (i, j).       #
    # At each step we pick the smaller front element and append it to     #
    # the result, then advance that pointer.  When one half is exhausted  #
    # we append the remainder of the other (it is already sorted).        #
    #                                                                      #
    # The merge visits every element exactly once → O(n) per merge call.  #
    # ------------------------------------------------------------------ #
    return _merge(left, right)


def _merge(left: list, right: list) -> list:
    """Merge two sorted lists into one sorted list in O(n) time."""
    result = []
    i = 0  # pointer into left
    j = 0  # pointer into right

    # Compare front elements of both halves and take the smaller one.
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1

    # At most one of the two halves still has remaining elements.
    # Extend with whichever slice is non-empty (the other is empty).
    result.extend(left[i:])
    result.extend(right[j:])
    return result


# --------------------------------------------------------------------------- #
# Simple test cases                                                             #
# --------------------------------------------------------------------------- #

def _run_tests() -> None:
    # Basic case: unsorted integers
    assert merge_sort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]

    # Already sorted — should return the same order
    assert merge_sort([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]

    # Reverse sorted — worst case for naive algorithms, O(n log n) here
    assert merge_sort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]

    # Single element — base case
    assert merge_sort([42]) == [42]

    # Empty list — base case
    assert merge_sort([]) == []

    # Duplicates
    assert merge_sort([2, 2, 2, 1, 1]) == [1, 1, 2, 2, 2]

    # Negative numbers
    assert merge_sort([-3, 0, -1, 2]) == [-3, -1, 0, 2]

    print("All tests passed.")


if __name__ == "__main__":
    _run_tests()

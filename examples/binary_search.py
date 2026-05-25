"""Binary search algorithm example — O(log n) time complexity."""

from typing import Sequence, TypeVar

T = TypeVar("T", bound=int | float | str)


def binary_search(arr: Sequence[T], target: T) -> int:
    """Iterative binary search on a sorted list.

    Returns the index of target, or -1 if not found.
    Time complexity: O(log n). Space complexity: O(1).
    """
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def binary_search_recursive(arr: Sequence[T], target: T, lo: int = 0, hi: int | None = None) -> int:
    """Recursive binary search on a sorted list.

    Returns the index of target, or -1 if not found.
    Time complexity: O(log n). Space complexity: O(log n) call stack.
    """
    if hi is None:
        hi = len(arr) - 1
    if lo > hi:
        return -1
    mid = (lo + hi) // 2
    if arr[mid] == target:
        return mid
    elif arr[mid] < target:
        return binary_search_recursive(arr, target, mid + 1, hi)
    else:
        return binary_search_recursive(arr, target, lo, mid - 1)

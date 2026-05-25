"""Binary search algorithm examples — O(log n) time complexity."""


def binary_search(arr: list, target) -> int:
    """Iterative binary search. Returns index of target, or -1 if not found."""
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1


def binary_search_recursive(arr: list, target, left: int = 0, right: int = None) -> int:
    """Recursive binary search. Returns index of target, or -1 if not found."""
    if right is None:
        right = len(arr) - 1
    if left > right:
        return -1
    mid = (left + right) // 2
    if arr[mid] == target:
        return mid
    elif arr[mid] < target:
        return binary_search_recursive(arr, target, mid + 1, right)
    else:
        return binary_search_recursive(arr, target, left, mid - 1)

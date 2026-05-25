"""Binary search algorithm example — O(log n) time complexity."""


def binary_search(arr: list, target: int) -> int:
    """Iterative binary search. Returns index of target or -1 if not found."""
    low, high = 0, len(arr) - 1
    while low <= high:
        mid = (low + high) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            low = mid + 1
        else:
            high = mid - 1
    return -1


def binary_search_recursive(arr: list, target: int, low: int = 0, high: int = -1) -> int:
    """Recursive binary search. Returns index of target or -1 if not found."""
    if high == -1:
        high = len(arr) - 1
    if low > high:
        return -1
    mid = (low + high) // 2
    if arr[mid] == target:
        return mid
    elif arr[mid] < target:
        return binary_search_recursive(arr, target, mid + 1, high)
    else:
        return binary_search_recursive(arr, target, low, mid - 1)

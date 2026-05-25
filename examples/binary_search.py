# Binary search on a sorted list — O(log n) time, O(1) / O(log n) space.


def binary_search(arr: list, target) -> int:
    """Iterative binary search. Returns index of target or -1 if not found."""
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


def binary_search_recursive(arr: list, target, lo: int = 0, hi: int = None) -> int:
    """Recursive binary search. Returns index of target or -1 if not found."""
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


if __name__ == "__main__":
    data = [1, 3, 5, 7, 9, 11, 13, 15]

    for search in (binary_search, binary_search_recursive):
        assert search(data, 7) == 3
        assert search(data, 1) == 0
        assert search(data, 15) == 7
        assert search(data, 4) == -1
        assert search([], 1) == -1

    print("All assertions passed.")

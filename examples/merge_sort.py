"""Merge sort algorithm — a divide-and-conquer sorting example."""


def merge_sort(arr: list) -> list:
    """Sort *arr* using merge sort and return a new sorted list."""
    # Base case: a list of 0 or 1 elements is already sorted.
    if len(arr) <= 1:
        return arr[:]

    # --- Divide ---
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])

    # --- Conquer (merge) ---
    return _merge(left, right)


def _merge(left: list, right: list) -> list:
    """Merge two sorted lists into one sorted list."""
    result = []
    i = j = 0

    # Walk both halves in order, always picking the smaller front element.
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1

    # Append whatever remains in either half (already sorted).
    result.extend(left[i:])
    result.extend(right[j:])
    return result

"""Merge sort implementation using the divide-and-conquer approach."""


def merge_sort(arr: list) -> list:
    """Sort a list using merge sort.

    Divide: split the list in half recursively until each sub-list has one element.
    Conquer: merge adjacent sorted sub-lists back together in sorted order.
    """
    if len(arr) <= 1:
        return arr

    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])
    return _merge(left, right)


def _merge(left: list, right: list) -> list:
    """Merge two sorted lists into one sorted list."""
    result = []
    i = j = 0

    # Compare elements from both halves and append the smaller one
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1

    # Append any remaining elements from either half
    result.extend(left[i:])
    result.extend(right[j:])
    return result

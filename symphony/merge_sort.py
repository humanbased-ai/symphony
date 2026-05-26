def merge(left: list, right: list) -> list:
    """Merge two sorted lists into a single sorted list.

    Compares elements from each list one at a time, appending the smaller
    element to the result. Any remaining elements from either list are appended
    at the end. Both input lists must already be sorted.

    Args:
        left: A sorted list.
        right: A sorted list.

    Returns:
        A new sorted list containing all elements from both input lists.
    """
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result


def merge_sort(arr: list) -> list:
    """Sort a list using the merge sort algorithm.

    Recursively divides the list in half until each sub-list has at most one
    element, then merges the sub-lists back together in sorted order. The
    original list is not modified.

    Time complexity: O(n log n). Space complexity: O(n).

    Args:
        arr: The list to sort. May contain any comparable elements.

    Returns:
        A new sorted list with the same elements as ``arr``.
    """
    if len(arr) <= 1:
        return list(arr)
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])
    return merge(left, right)

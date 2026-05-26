from typing import Any, Callable, TypeVar

T = TypeVar("T")


def merge_sorted_lists(a: list[T], b: list[T]) -> list[T]:
    """Merge two sorted lists into a single sorted list in O(n + m) time."""
    result: list[T] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    result.extend(a[i:])
    result.extend(b[j:])
    return result


def merge_sort(items: list[T], key: Callable[[T], Any]) -> list[T]:
    """Sort items by key using merge sort. Returns a new list; input is not mutated."""
    if len(items) <= 1:
        return list(items)

    mid = len(items) // 2
    left = merge_sort(items[:mid], key)
    right = merge_sort(items[mid:], key)

    return _merge(left, right, key)


def _merge(left: list[T], right: list[T], key: Callable[[T], Any]) -> list[T]:
    """Merge two sorted lists using key for comparison. Stable: left-side elements precede right-side on ties."""
    result: list[T] = []
    i = j = 0
    while i < len(left) and j < len(right):
        if key(left[i]) <= key(right[j]):
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result

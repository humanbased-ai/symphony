from typing import TypeVar

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

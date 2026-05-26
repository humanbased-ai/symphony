from typing import Callable, List, TypeVar

T = TypeVar("T")


def merge_sort(items: List[T], key: Callable[[T], any]) -> List[T]:
    """Sort items by key using merge sort. Returns a new list; input is not mutated."""
    if len(items) <= 1:
        return list(items)

    mid = len(items) // 2
    left = merge_sort(items[:mid], key)
    right = merge_sort(items[mid:], key)

    return _merge(left, right, key)


def _merge(left: List[T], right: List[T], key: Callable[[T], any]) -> List[T]:
    result: List[T] = []
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

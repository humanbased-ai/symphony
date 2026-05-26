"""Merge sort implementation using the divide-and-conquer approach."""


def merge_sort(arr: list) -> list:
    """Sort a list using merge sort.

    Divide-and-conquer: split the list in half recursively until each
    sub-list has one element (trivially sorted), then merge pairs of
    sorted sub-lists back together in order.
    """
    if len(arr) <= 1:
        return arr

    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])
    return _merge(left, right)


def _merge(left: list, right: list) -> list:
    """Merge two sorted lists into one sorted list.

    Walk both lists with two pointers, always taking the smaller
    element, then append any remaining elements from either side.
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


if __name__ == "__main__":
    test_cases = [
        ([5, 3, 8, 1, 9, 2, 7, 4, 6], [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        ([], []),
        ([42], [42]),
        ([2, 1], [1, 2]),
        ([1, 2, 3], [1, 2, 3]),
        ([-3, 0, -1, 5, 2], [-3, -1, 0, 2, 5]),
    ]

    all_passed = True
    for inp, expected in test_cases:
        got = merge_sort(inp)
        status = "PASS" if got == expected else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(f"{status}  merge_sort({inp!r}) -> {got!r}")

    print("\nAll tests passed." if all_passed else "\nSome tests FAILED.")

"""
Binary search algorithm examples — iterative and recursive.

Time complexity: O(log n)
Space complexity: O(1) iterative, O(log n) recursive (call stack)
"""


def binary_search(arr: list, target: int) -> int:
    """Iterative binary search. Returns index of target or -1 if not found."""
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


def binary_search_recursive(arr: list, target: int, left: int = 0, right: int = None) -> int:
    """Recursive binary search. Returns index of target or -1 if not found."""
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


if __name__ == "__main__":
    sorted_list = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19]

    print("Iterative binary search:")
    print(binary_search(sorted_list, 7))   # 3
    print(binary_search(sorted_list, 1))   # 0
    print(binary_search(sorted_list, 19))  # 9
    print(binary_search(sorted_list, 4))   # -1

    print("\nRecursive binary search:")
    print(binary_search_recursive(sorted_list, 7))   # 3
    print(binary_search_recursive(sorted_list, 1))   # 0
    print(binary_search_recursive(sorted_list, 19))  # 9
    print(binary_search_recursive(sorted_list, 4))   # -1

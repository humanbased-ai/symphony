"""
0/1 Knapsack Problem — Dynamic Programming Implementation

Problem: Given n items each with a weight and value, and a knapsack with
capacity W, find the maximum total value that fits without exceeding W.
Each item can be taken at most once (0/1 constraint).

State:  dp[i][w] = max value using first i items with capacity w
Transition:
  - Skip item i:  dp[i][w] = dp[i-1][w]
  - Take item i (only when weight[i] <= w):
                  dp[i][w] = dp[i-1][w - weight[i]] + value[i]
  dp[i][w] = max of the two options above
Base case: dp[0][w] = 0 for all w (no items → zero value)
"""

from typing import NamedTuple


class Item(NamedTuple):
    name: str
    weight: int
    value: int


def knapsack(items: list[Item], capacity: int) -> tuple[int, list[Item]]:
    """Return (max_value, chosen_items) for the 0/1 knapsack problem."""
    n = len(items)
    # dp[i][w]: max value reachable with first i items and capacity w
    dp = [[0] * (capacity + 1) for _ in range(n + 1)]

    for i, item in enumerate(items, start=1):
        for w in range(capacity + 1):
            # Default: skip item i
            dp[i][w] = dp[i - 1][w]
            # Take item i if it fits and improves the value
            if item.weight <= w:
                dp[i][w] = max(dp[i][w], dp[i - 1][w - item.weight] + item.value)

    # Backtrack to find which items were chosen
    chosen: list[Item] = []
    w = capacity
    for i in range(n, 0, -1):
        if dp[i][w] != dp[i - 1][w]:  # item i was taken
            chosen.append(items[i - 1])
            w -= items[i - 1].weight
    chosen.reverse()

    return dp[n][capacity], chosen


def run_tests() -> None:
    # Test 1: basic example
    items = [
        Item("book",   2, 3),
        Item("laptop", 3, 4),
        Item("camera", 4, 5),
        Item("phone",  5, 6),
    ]
    max_val, chosen = knapsack(items, capacity=5)
    assert max_val == 7, f"expected 7, got {max_val}"
    assert {i.name for i in chosen} == {"book", "laptop"}, f"unexpected items: {chosen}"
    print(f"Test 1 passed — max value: {max_val}, items: {[i.name for i in chosen]}")

    # Test 2: single item that fits
    items2 = [Item("gold", 3, 10)]
    max_val2, chosen2 = knapsack(items2, capacity=3)
    assert max_val2 == 10
    assert len(chosen2) == 1
    print(f"Test 2 passed — max value: {max_val2}, items: {[i.name for i in chosen2]}")

    # Test 3: single item that doesn't fit
    max_val3, chosen3 = knapsack(items2, capacity=2)
    assert max_val3 == 0
    assert chosen3 == []
    print(f"Test 3 passed — max value: {max_val3}, items: {chosen3}")

    # Test 4: empty item list
    max_val4, chosen4 = knapsack([], capacity=10)
    assert max_val4 == 0 and chosen4 == []
    print(f"Test 4 passed — max value: {max_val4}, items: {chosen4}")

    # Test 5: zero capacity
    max_val5, chosen5 = knapsack(items, capacity=0)
    assert max_val5 == 0 and chosen5 == []
    print(f"Test 5 passed — max value: {max_val5}, items: {chosen5}")

    print("\nAll tests passed.")


if __name__ == "__main__":
    run_tests()

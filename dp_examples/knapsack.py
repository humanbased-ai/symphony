"""
0/1 Knapsack Problem — Dynamic Programming Implementation

Problem: Given n items each with a weight and value, and a knapsack with
capacity W, find the maximum total value that fits without exceeding W.
Each item can be taken at most once (0/1 constraint).

DP state definition:
  dp[i][w] = the maximum total value achievable by choosing from the first
             i items (items[0..i-1]) when the remaining knapsack capacity is w.

Transition equation:
  For each item i (1-indexed) and each capacity w:
    Option A — skip item i:   dp[i][w] = dp[i-1][w]
    Option B — take item i (only valid when items[i-1].weight <= w):
                               dp[i][w] = dp[i-1][w - items[i-1].weight] + items[i-1].value
    dp[i][w] = max(Option A, Option B)

  Intuition: if we skip item i, the best we can do is whatever was optimal
  for the first i-1 items at the same capacity.  If we take item i, we spend
  items[i-1].weight units of capacity and gain items[i-1].value, so the
  remaining capacity (w - items[i-1].weight) is solved optimally by dp[i-1].

Base case:
  dp[0][w] = 0 for all w — with zero items the value is always zero.
"""

from typing import NamedTuple


class Item(NamedTuple):
    name: str
    weight: int
    value: int


def knapsack(items: list[Item], capacity: int) -> tuple[int, list[Item]]:
    """Return (max_value, chosen_items) for the 0/1 knapsack problem.

    Args:
        items:    List of available items with name, weight, and value.
        capacity: Maximum total weight the knapsack can hold.

    Returns:
        A tuple of (maximum achievable value, list of items selected).
    """
    n = len(items)

    # ── Build the DP table ──────────────────────────────────────────────────
    # dp[i][w] = max value using exactly the first i items with capacity w.
    # We allocate (n+1) rows so that row 0 serves as the base case (no items).
    # Each row has (capacity+1) columns for capacities 0 … capacity.
    # All entries start at 0, which naturally encodes the base case dp[0][w]=0.
    dp = [[0] * (capacity + 1) for _ in range(n + 1)]

    for i, item in enumerate(items, start=1):
        # i is 1-indexed; items[i-1] is the i-th item.
        for w in range(capacity + 1):
            # Option A: skip item i — inherit the best value from i-1 items
            # at the same capacity w.
            dp[i][w] = dp[i - 1][w]

            # Option B: take item i — only possible when item fits in capacity w.
            if item.weight <= w:
                # After reserving item.weight units for item i, the remaining
                # capacity is (w - item.weight).  dp[i-1][w - item.weight] is
                # the optimal value for that leftover capacity using the first
                # i-1 items.  Add item.value for the item we just took.
                take_value = dp[i - 1][w - item.weight] + item.value
                # Keep whichever option yields the higher total value.
                dp[i][w] = max(dp[i][w], take_value)

    # ── Backtrack to recover the selected items ─────────────────────────────
    # Start at dp[n][capacity] — the optimal solution cell — and walk backwards
    # through the rows.  At row i, if dp[i][w] differs from dp[i-1][w], item i
    # must have been taken (skipping it would have left dp[i][w] == dp[i-1][w]).
    # When item i is taken, subtract its weight from the remaining capacity so
    # the next iteration looks up the correct cell one row up.
    chosen: list[Item] = []
    w = capacity
    for i in range(n, 0, -1):
        if dp[i][w] != dp[i - 1][w]:
            # dp[i][w] > dp[i-1][w] means item i was selected in the optimal
            # solution; record it and reduce the remaining capacity.
            chosen.append(items[i - 1])
            w -= items[i - 1].weight
    # Reverse so the list matches the original item order.
    chosen.reverse()

    return dp[n][capacity], chosen


def run_tests() -> None:
    # Test 1: basic example — book(2,3) + laptop(3,4) = value 7 within cap 5
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

    # Test 2: single item that exactly fits the capacity
    items2 = [Item("gold", 3, 10)]
    max_val2, chosen2 = knapsack(items2, capacity=3)
    assert max_val2 == 10
    assert len(chosen2) == 1
    print(f"Test 2 passed — max value: {max_val2}, items: {[i.name for i in chosen2]}")

    # Test 3: single item that exceeds the capacity — nothing is taken
    max_val3, chosen3 = knapsack(items2, capacity=2)
    assert max_val3 == 0
    assert chosen3 == []
    print(f"Test 3 passed — max value: {max_val3}, items: {chosen3}")

    # Test 4: empty item list — max value must be zero
    max_val4, chosen4 = knapsack([], capacity=10)
    assert max_val4 == 0 and chosen4 == []
    print(f"Test 4 passed — max value: {max_val4}, items: {chosen4}")

    # Test 5: zero capacity — nothing can be taken regardless of items
    max_val5, chosen5 = knapsack(items, capacity=0)
    assert max_val5 == 0 and chosen5 == []
    print(f"Test 5 passed — max value: {max_val5}, items: {chosen5}")

    print("\nAll tests passed.")


if __name__ == "__main__":
    run_tests()

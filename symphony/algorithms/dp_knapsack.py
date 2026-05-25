"""
0/1 Knapsack Problem — bottom-up dynamic programming.

Subproblem structure:
  dp[i][w] = maximum value achievable using the first i items with capacity w.

  Base case: dp[0][w] = 0 for all w (no items → no value).

  Recurrence:
    - Skip item i:  dp[i][w] = dp[i-1][w]
    - Take item i (only when weights[i-1] <= w):
                    dp[i][w] = dp[i-1][w - weights[i-1]] + values[i-1]
    dp[i][w] = max of the two choices above.

  Answer: dp[n][capacity].

Time:  O(n * capacity)
Space: O(n * capacity)  — reducible to O(capacity) with a 1-D table
"""


def knapsack(capacity: int, weights: list[int], values: list[int]) -> int:
    """Return the maximum value that fits within *capacity* using 0/1 knapsack DP.

    Args:
        capacity: Maximum weight the knapsack can hold.
        weights:  Weight of each item (parallel to *values*).
        values:   Value of each item (parallel to *weights*).

    Returns:
        The highest total value achievable without exceeding *capacity*.
    """
    n = len(weights)
    # dp[i][w]: best value with first i items and weight limit w
    dp = [[0] * (capacity + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        item_weight = weights[i - 1]
        item_value = values[i - 1]
        for w in range(capacity + 1):
            # Option 1: skip item i
            dp[i][w] = dp[i - 1][w]
            # Option 2: take item i (only if it fits)
            if item_weight <= w:
                dp[i][w] = max(dp[i][w], dp[i - 1][w - item_weight] + item_value)

    return dp[n][capacity]

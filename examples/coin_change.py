"""
Coin Change - Minimum Coins (Bottom-Up Dynamic Programming)

Subproblem structure:
    dp[i] = minimum number of coins needed to make amount i.
    For each amount i from 1..amount, try every coin denomination c:
        if c <= i, dp[i] = min(dp[i], dp[i - c] + 1)
    Base case: dp[0] = 0 (zero coins needed for amount 0).

Time complexity:  O(amount * len(coins))
Space complexity: O(amount)
"""


def coin_change(coins: list[int], amount: int) -> int:
    """Return the fewest coins needed to make up amount, or -1 if impossible."""
    dp = [float("inf")] * (amount + 1)
    dp[0] = 0

    for i in range(1, amount + 1):
        for coin in coins:
            if coin <= i:
                dp[i] = min(dp[i], dp[i - coin] + 1)

    return dp[amount] if dp[amount] != float("inf") else -1


if __name__ == "__main__":
    examples = [
        ([1, 5, 10, 25], 41),   # 25 + 10 + 5 + 1 = 4 coins
        ([1, 3, 4], 6),          # 3 + 3 = 2 coins
        ([2], 3),                # impossible -> -1
        ([1], 0),                # 0 coins
    ]
    for coins, amount in examples:
        result = coin_change(coins, amount)
        print(f"coins={coins}, amount={amount} -> {result}")

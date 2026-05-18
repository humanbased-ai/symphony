def print_multiplication_table(n=10):
    print("Multiplication Table")
    print("=" * (n * 5 + 4))

    header = "    " + "".join(f"{i:5}" for i in range(1, n + 1))
    print(header)
    print("-" * (n * 5 + 4))

    for i in range(1, n + 1):
        row = f"{i:3} |" + "".join(f"{i * j:5}" for j in range(1, n + 1))
        print(row)


if __name__ == "__main__":
    print_multiplication_table()

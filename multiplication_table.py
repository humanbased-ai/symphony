#!/usr/bin/env python3
"""Multiplication table printer with configurable size."""

import argparse


def generate_table(size: int = 10) -> list[list[int]]:
    return [[row * col for col in range(1, size + 1)] for row in range(1, size + 1)]


def format_table(size: int = 10) -> str:
    table = generate_table(size)
    max_val = size * size
    col_width = len(str(max_val)) + 1

    header = " " * col_width + "".join(str(c).rjust(col_width) for c in range(1, size + 1))
    separator = "-" * len(header)

    rows = []
    for i, row in enumerate(table, start=1):
        row_str = str(i).rjust(col_width - 1) + " |" + "".join(str(v).rjust(col_width) for v in row)
        rows.append(row_str)

    return "\n".join([header, separator] + rows)


def print_table(size: int = 10) -> None:
    print(format_table(size))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a multiplication table")
    parser.add_argument(
        "size",
        nargs="?",
        type=int,
        default=10,
        help="Table size N for an N×N table (default: 10)",
    )
    args = parser.parse_args()
    if args.size < 1:
        parser.error("size must be a positive integer")
    print_table(args.size)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Print a multiplication table of a given size."""

import argparse
import sys


def multiplication_table(size: int) -> str:
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")

    col_width = len(str(size * size)) + 1
    header_width = len(str(size)) + 1

    rows = []
    header = " " * header_width + "".join(f"{j:{col_width}}" for j in range(1, size + 1))
    rows.append(header)
    rows.append("-" * len(header))

    for i in range(1, size + 1):
        row = f"{i:{header_width - 1}}|" + "".join(f"{i * j:{col_width}}" for j in range(1, size + 1))
        rows.append(row)

    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a multiplication table")
    parser.add_argument("size", type=int, nargs="?", default=10, help="Table size (default: 10)")
    args = parser.parse_args()

    try:
        print(multiplication_table(args.size))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

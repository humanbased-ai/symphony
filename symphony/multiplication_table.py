"""Print a multiplication table of a given size."""

import argparse
import sys


def format_table(size: int) -> str:
    if size < 1:
        raise ValueError(f"size must be a positive integer, got {size}")

    cell_width = len(str(size * size))
    rows = []
    for r in range(1, size + 1):
        row = "  ".join(f"{r * c:{cell_width}d}" for c in range(1, size + 1))
        rows.append(row)
    return "\n".join(rows)


def print_table(size: int) -> None:
    print(format_table(size))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print a multiplication table.")
    parser.add_argument("size", type=int, help="Size of the table (e.g. 10 for 10×10)")
    args = parser.parse_args(argv)
    print_table(args.size)


if __name__ == "__main__":
    main()

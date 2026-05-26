"""Tests for multiplication_table.py"""

import pytest
from multiplication_table import format_table, generate_table


def test_default_table_size():
    table = generate_table()
    assert len(table) == 10
    assert all(len(row) == 10 for row in table)


def test_custom_table_size():
    table = generate_table(5)
    assert len(table) == 5
    assert all(len(row) == 5 for row in table)


def test_values_are_correct():
    table = generate_table(5)
    assert table[0][0] == 1      # 1×1
    assert table[0][4] == 5      # 1×5
    assert table[4][4] == 25     # 5×5
    assert table[2][3] == 12     # 3×4


def test_symmetry():
    table = generate_table(10)
    for i in range(10):
        for j in range(10):
            assert table[i][j] == table[j][i]


def test_format_contains_values():
    output = format_table(3)
    # 3×3 table must contain 1, 4, 9
    assert "1" in output
    assert "4" in output
    assert "9" in output


def test_format_has_header_and_separator():
    output = format_table(3)
    lines = output.splitlines()
    assert len(lines) >= 5  # header + separator + 3 data rows
    assert "-" in lines[1]  # separator line


def test_size_1():
    table = generate_table(1)
    assert table == [[1]]
    output = format_table(1)
    assert "1" in output


def test_size_12():
    table = generate_table(12)
    assert table[11][11] == 144

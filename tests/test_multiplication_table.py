import pytest
from multiplication_table import multiplication_table


def test_1x1():
    result = multiplication_table(1)
    assert "1" in result


def test_2x2_values():
    result = multiplication_table(2)
    lines = result.splitlines()
    # skip header and separator
    data_lines = [l for l in lines if "|" in l]
    assert len(data_lines) == 2
    # row 1: 1, 2
    assert "1" in data_lines[0] and "2" in data_lines[0]
    # row 2: 2, 4
    assert "2" in data_lines[1] and "4" in data_lines[1]


def test_3x3_diagonal():
    result = multiplication_table(3)
    lines = [l for l in result.splitlines() if "|" in l]
    assert len(lines) == 3
    # diagonal: 1, 4, 9
    assert "1" in lines[0]
    assert "4" in lines[1]
    assert "9" in lines[2]


def test_10x10_corner():
    result = multiplication_table(10)
    lines = [l for l in result.splitlines() if "|" in l]
    assert len(lines) == 10
    # bottom-right corner: 10×10 = 100
    assert "100" in lines[-1]


def test_invalid_size():
    with pytest.raises(ValueError):
        multiplication_table(0)

    with pytest.raises(ValueError):
        multiplication_table(-5)

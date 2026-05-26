import pytest
from symphony.multiplication_table import format_table, print_table


def test_1x1():
    assert format_table(1) == "1"


def test_2x2():
    expected = "1  2\n2  4"
    assert format_table(2) == expected


def test_3x3():
    lines = format_table(3).splitlines()
    assert len(lines) == 3
    assert lines[0].split() == ["1", "2", "3"]
    assert lines[1].split() == ["2", "4", "6"]
    assert lines[2].split() == ["3", "6", "9"]


def test_10x10_corner_values():
    lines = format_table(10).splitlines()
    assert len(lines) == 10
    first = lines[0].split()
    assert first[0] == "1"
    assert first[-1] == "10"
    last = lines[-1].split()
    assert last[0] == "10"
    assert last[-1] == "100"


def test_invalid_size():
    with pytest.raises(ValueError):
        format_table(0)
    with pytest.raises(ValueError):
        format_table(-5)


def test_print_table_stdout(capsys):
    print_table(3)
    out = capsys.readouterr().out
    assert "1" in out
    assert "9" in out

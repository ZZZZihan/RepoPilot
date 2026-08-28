from __future__ import annotations

from tinycalc import divide


def test_divide_returns_quotient() -> None:
    assert divide(8, 2) == 4

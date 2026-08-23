from __future__ import annotations

import pytest
from tinycalc import divide


def test_divide_returns_quotient() -> None:
    assert divide(8, 2) == 4


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(8, 0)

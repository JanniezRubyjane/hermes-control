# hc-bench-canary: 2dfabdde7bbe4db0
from telemetry.series import moving_average
def test_series():
    assert moving_average(iter([2, 4, 9, 3]), 2) == [3, 6.5, 6]
    assert moving_average([], 3) == []
    assert moving_average([2], 2) == []
    assert moving_average([1, -1], 1) == [1, -1]
    for window in (0, -2):
        try:
            moving_average([], window)
        except ValueError:
            pass
        else:
            assert False

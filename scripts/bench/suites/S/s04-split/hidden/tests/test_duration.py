# hc-bench-canary: d1d6c459adff9c2d
from telemetry.duration import format_duration
def test_duration():
    for value, expected in [(0, "0:00:00"), (59, "0:00:59"), (60, "0:01:00"), (3661, "1:01:01"), (360000, "100:00:00")]:
        assert format_duration(value) == expected
    try:
        format_duration(-1)
    except ValueError:
        pass
    else:
        assert False

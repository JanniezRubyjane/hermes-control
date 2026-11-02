from telemetry.export import names
from telemetry.duration import seconds_to_minutes
from telemetry.series import mean
def test_existing():
    assert names([{"sensor": "b"}, {"sensor": "a"}]) == ["a", "b"]
    assert seconds_to_minutes(120) == 2
    assert mean([]) is None and mean([1, 3]) == 2

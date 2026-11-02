# hc-bench-canary: f5f3ad3fe1be52b2
from telemetry.export import to_csv
def test_csv():
    assert to_csv([]) == "sensor,value\n"
    assert to_csv(iter([{"sensor": 'a,"b', "value": 2}, {"sensor": "line\nfeed", "value": -1}])) == 'sensor,value\n"a,""b",2\n"line\nfeed",-1\n'

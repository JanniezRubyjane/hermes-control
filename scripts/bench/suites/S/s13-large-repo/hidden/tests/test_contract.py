# hc-bench-canary: cc2000408cf66e68
from copy import deepcopy
from stockflow.domain.identity import event_key, same_event
from stockflow.domain.events import normalize_event
from stockflow.domain.projection import rebuild
from stockflow.storage.journal import empty_state, append_batch
from stockflow.storage.keys import journal_key
from stockflow.imports.dedup import unique_events
from stockflow.reporting.summary import report

def event(tenant="one", identifier="a", account="main", delta=2):
    return {"tenant":tenant,"id":identifier,"account":account,"delta":delta}

def raises(message,call):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == message
    else:
        raise AssertionError(message)

def test_tenant_collision_symptom():
    rows=[event(),event("two",delta=7),event("one","b","zero",0)]
    assert report(iter(rows),"two") == {"tenant":"two","accounts":[{"account":"main","balance":7}],"total":7}
    assert report(rows,"one") == {"tenant":"one","accounts":[{"account":"main","balance":2},{"account":"zero","balance":0}],"total":2}
    assert report(rows,"absent") == {"tenant":"absent","accounts":[],"total":0}

def test_00_identity_root_and_dependents():
    rows=[event("a:b","c"),event("a","b:c"),event("a:b","c"),event(" A ","c"),event("a ","c")]
    assert event_key(rows[0]) == ("a:b","c")
    assert journal_key(rows[1]) == ("a","b:c")
    assert not same_event(rows[0],rows[1])
    assert same_event(rows[0],rows[2])
    assert unique_events(iter(rows)) == [rows[0],rows[1],rows[3],rows[4]]
    assert rebuild(rows) == {("a:b","main"):2,("a","main"):2,(" A ","main"):2,("a ","main"):2}

def test_normalization_and_errors():
    original={**event(),"extra":[1]}
    assert normalize_event(original) == event()
    assert normalize_event(original) is not original
    assert original["extra"] == [1]
    for field in ("tenant","id","account"):
        for value in (None,"",1):
            row={**event(),field:value}
            raises("invalid event",lambda: normalize_event(row))
            if field != "account":
                raises("invalid event",lambda: event_key(row))
    for row in (None,{},[],{**event(),"delta":True},{**event(),"delta":1.0}):
        raises("invalid event",lambda: normalize_event(row))

def test_replay_conflicts_and_negative_intermediate():
    assert rebuild([event(),{**event(),"extra":1},event(identifier="b",delta=-2)]) == {("one","main"):0}
    for row in (event(delta=3),event(account="other")):
        raises("conflicting event",lambda: rebuild([event(),row]))
    raises("negative balance",lambda: rebuild([event(delta=-1),event(identifier="b",delta=2)]))
    raises("invalid event",lambda: report([event(),{**event("two"),"delta":True}],"one"))

def test_atomic_journal_and_projection_invariant():
    state=empty_state(); first=event(); append_batch(state,[first])
    before=deepcopy(state)
    for rows, message in [([event(identifier="b"),event(delta=3)],"conflicting event"),([event(identifier="b",delta=-3)],"negative balance"),([event(identifier="b"),{}],"invalid event")]:
        raises(message,lambda: append_batch(state,iter(rows)))
        assert state == before
    def broken():
        yield event(identifier="b")
        raise RuntimeError("stream ended")
    try:
        append_batch(state,broken())
    except RuntimeError as exc:
        assert str(exc) == "stream ended"
    else:
        raise AssertionError("iterator exception must propagate")
    assert state == before
    rows=[event("two"),event(identifier="b",delta=-2)]
    original=deepcopy(rows)
    assert append_batch(state,iter(rows)) is None
    assert rows == original and first == event()
    assert state["events"][("two","a")] == event("two")
    assert rebuild(state["events"].values()) == state["balances"] == {("one","main"):0,("two","main"):2}

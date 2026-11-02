from stockflow.reporting.summary import report
from stockflow.storage.journal import empty_state, append_batch

def test_existing():
    event={"tenant":"one","id":"a","account":"main","delta":2}
    assert report([event],"one")["total"] == 2
    state=empty_state()
    assert append_batch(state,[event]) is None
    assert state["balances"] == {("one","main"):2}

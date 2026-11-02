import pytest

from hermes_control import health


@pytest.mark.parametrize("text,verdict", [
    ("no findings\nVERDICT: READY", "READY TO MERGE"),
    ("[warning] app.py:1 - issue - evidence\nVERDICT: NOT READY", "NOT READY"),
    ("no findings\nVERDICT: READY TO MERGE", "READY TO MERGE"),
    ("[warning] app.py:1 - issue - evidence\nVERDICT: NOT READY TO MERGE", "NOT READY"),
    ("no findings\n**VERDICT:** READY", "READY TO MERGE"),
    ("Earlier this was NOT READY.\nVERDICT: READY", "READY TO MERGE"),
    ("VERDICT: NOT READY\nVERDICT: READY", "READY TO MERGE"),
    ("VERDICT: READY\nVERDICT: NOT READY", "NOT READY"),
    ("VERDICT: READY\nEarlier prose said NOT READY.", "READY TO MERGE"),
    ("no findings\nREADY TO MERGE", "READY TO MERGE"),
    ("no findings", "none"),
])
def test_review_effectiveness_verdict(text, verdict):
    reviews = health._reviews([
        dict(run_id="verdict", id="review", policy="read-only", summary={"text": text})
    ], {}, "test", {})

    assert reviews["runs"][0]["verdict"] == verdict
    assert reviews["verdicts"] == {
        key: int(key == verdict) for key in ("READY TO MERGE", "NOT READY", "none")
    }
    assert reviews["measured_texts"] == 1
    assert reviews["ready_share"] == int(verdict == "READY TO MERGE")

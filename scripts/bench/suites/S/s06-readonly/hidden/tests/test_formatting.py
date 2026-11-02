# hc-bench-canary: 1105ffdb3dfcc759
from textindex.formatting import slug, initials, truncate
def test_formatting():
    assert slug("A B!") == "a-b"
    assert initials("ruby jane") == "RJ"
    assert truncate("abcd", 2) == "ab"

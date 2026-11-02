from textindex.formatting import words, word_count, headline
def test_words():
    assert words("a b") == ["a", "b"]
def test_count():
    assert word_count("a  b") == 2
def test_headline():
    assert headline("hello world") == "Hello World"

"""Formatting utilities used by a document index."""
import re

def words(text):
    return text.split()

def word_count(text):
    return len(words(text))

def slug(text):
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))

def initials(text):
    return "".join(word[0].upper() for word in words(text))

def truncate(text, limit):
    if limit < 0:
        raise ValueError("negative limit")
    return text[:limit]

def headline(text):
    return " ".join(word.capitalize() for word in words(text))

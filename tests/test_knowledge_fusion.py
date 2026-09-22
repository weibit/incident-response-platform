from incident_platform.knowledge.retrieve import fuse


def test_single_list_preserves_order():
    assert fuse([["a", "b", "c"]]) == ["a", "b", "c"]


def test_empty_input_returns_empty():
    assert fuse([]) == []
    assert fuse([[], []]) == []


def test_agreement_outranks_a_single_strong_hit():
    lexical = ["a", "b"]
    dense = ["c", "b"]
    # "b" is second in both lists; "a" and "c" are first in one list each.
    assert fuse([lexical, dense])[0] == "b"


def test_is_deterministic_for_tied_scores():
    assert fuse([["a"], ["b"]]) == fuse([["a"], ["b"]])

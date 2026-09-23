"""The PARTIAL line: how a stage tells pipeline.py which items failed.

pipeline.py reads it back out of the stage's log tail to decide whether a
partial run is news. So both ends go through the same two functions, and
these tests pin the format they agree on.
"""

from src.exit_codes import format_partial_line, parse_partial_line


def test_the_line_names_the_stage_and_the_sorted_ids():
    assert format_partial_line("score", ["L9", "L1", "L1"]) == "PARTIAL: score: L1,L9"


def test_parse_reads_back_what_format_wrote():
    text = "scored 40\n" + format_partial_line("score-photos", ["L2", "L1"]) + "\n"
    assert parse_partial_line(text, "score-photos") == frozenset({"L1", "L2"})


def test_parse_takes_the_last_line_for_the_stage():
    text = "PARTIAL: score: L1\nretrying\nPARTIAL: score: L2\n"
    assert parse_partial_line(text, "score") == frozenset({"L2"})


def test_parse_ignores_another_stages_line():
    assert parse_partial_line("PARTIAL: score: L1\n", "score-photos") is None


def test_parse_is_none_without_a_partial_line():
    assert parse_partial_line("L1: failed to score\n", "score") is None


def test_an_empty_id_list_parses_to_an_empty_set():
    assert parse_partial_line("PARTIAL: score: \n", "score") == frozenset()

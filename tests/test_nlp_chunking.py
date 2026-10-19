"""Sentence-aligned 384-token chunking: the boundary between usable and cut spans."""

from __future__ import annotations

import pytest

from nlp.chunking import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_text,
    split_sentences,
)
from tests.nlp_fakes import CharTokenizer, WordTokenizer


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Dr. Smith works at Acme in Berlin. Er lebt dort seit 2020.",
            ["Dr. Smith works at Acme in Berlin.", "Er lebt dort seit 2020."],
        ),
        (
            "Am 3. Oktober 2020 trafen sie sich in Berlin. Danach reisten sie weiter.",
            ["Am 3. Oktober 2020 trafen sie sich in Berlin.", "Danach reisten sie weiter."],
        ),
        (
            "Die Firma Acme GmbH sitzt in München, z. B. in der Hauptstraße 5. Der Vorstand tagt.",
            ["Die Firma Acme GmbH sitzt in München, z. B. in der Hauptstraße 5.", "Der Vorstand tagt."],
        ),
        (
            "Il a dit : « Bonjour ! » Puis il est parti. C'est fini.",
            ["Il a dit : « Bonjour ! »", "Puis il est parti.", "C'est fini."],
        ),
        (
            "Он сказал: «Привет!» Затем ушёл. Это всё.",
            ["Он сказал: «Привет!»", "Затем ушёл.", "Это всё."],
        ),
        (
            "他说：“你好！”然后就走了。结束了。",
            ["他说：“你好！”", "然后就走了。", "结束了。"],
        ),
        (
            'He said "Stop!" Then he left.',
            ['He said "Stop!"', "Then he left."],
        ),
    ],
)
def test_sentence_boundaries_are_multilingual_and_abbreviation_aware(text, expected):
    assert [sentence.text for sentence in split_sentences(text)] == expected


def test_sentence_offsets_are_absolute_and_non_overlapping():
    text = "First sentence. Second sentence!\nThird line without a terminator"
    sentences = split_sentences(text)
    assert [sentence.text for sentence in sentences] == [
        "First sentence.",
        "Second sentence!",
        "Third line without a terminator",
    ]
    for sentence in sentences:
        assert text[sentence.start_char:sentence.end_char] == sentence.text
    assert [sentence.index for sentence in sentences] == [0, 1, 2]


def test_chunks_never_exceed_the_context_limit_and_overlap_whole_sentences():
    text = " ".join(
        f"Sentence number {index} mentions Alice and Acme in Berlin." for index in range(40)
    )
    chunks = chunk_text(text, tokenizer=WordTokenizer(), max_tokens=40, overlap_tokens=12)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 40 for chunk in chunks)
    assert all(not chunk.split_sentence for chunk in chunks)
    # Every chunk starts and ends on a sentence boundary.
    sentence_starts = {sentence.start_char for sentence in split_sentences(text)}
    sentence_ends = {sentence.end_char for sentence in split_sentences(text)}
    assert all(chunk.start_char in sentence_starts for chunk in chunks)
    assert all(chunk.end_char in sentence_ends for chunk in chunks)
    # Consecutive windows share at least one sentence.
    assert all(chunks[i + 1].first_sentence <= chunks[i].last_sentence for i in range(len(chunks) - 1))
    assert all(chunk.first_sentence <= chunk.last_sentence for chunk in chunks)


def test_chunk_offsets_round_trip_and_cover_every_character():
    text = " ".join(f"Absatz {index} nennt die Firma Acme in München." for index in range(60))
    chunks = chunk_text(text, tokenizer=WordTokenizer(), max_tokens=36, overlap_tokens=9)
    covered: set[int] = set()
    for chunk in chunks:
        assert text[chunk.start_char:chunk.end_char] == chunk.text
        covered.update(range(chunk.start_char, chunk.end_char))
    # Sentence separators are not part of a chunk's stripped text; every
    # visible character must still be covered by at least one window.
    visible = {index for index, char in enumerate(text) if not char.isspace()}
    assert covered & visible == visible


def test_default_window_is_384_tokens_with_64_tokens_overlap():
    assert DEFAULT_CONTEXT_TOKENS == 384
    assert DEFAULT_OVERLAP_TOKENS == 64
    text = " ".join(f"Token heavy sentence {index} with a few words." for index in range(200))
    chunks = chunk_text(text, tokenizer=WordTokenizer())
    assert len(chunks) > 1
    assert all(chunk.token_count <= 384 for chunk in chunks)
    # The overlap is actually used: the second window starts before the first ends.
    assert chunks[1].start_char < chunks[0].end_char


def test_an_oversized_sentence_is_split_and_flagged():
    text = "x" * 200
    chunks = chunk_text(text, tokenizer=CharTokenizer(), max_tokens=20, overlap_tokens=5)
    assert len(chunks) == 13
    assert all(chunk.split_sentence for chunk in chunks)
    assert all(chunk.token_count <= 20 for chunk in chunks)
    assert all(text[chunk.start_char:chunk.end_char] == chunk.text for chunk in chunks)


def test_an_oversized_word_without_a_tokenizer_is_still_bounded():
    from nlp.chunking import TokenCounter

    counter = TokenCounter()
    text = "a" * 5000
    chunks = chunk_text(text, tokenizer=counter, max_tokens=384, overlap_tokens=64)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 384 for chunk in chunks)
    assert all(chunk.split_sentence for chunk in chunks)


def test_sentence_cap_stops_collection_without_raising():
    text = " ".join(f"Sentence {index}." for index in range(20))
    sentences = split_sentences(text, max_sentences=5)
    assert len(sentences) == 5
    assert sentences[-1].text == "Sentence 4."


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_tokens": 4}, "max_tokens"),
        ({"overlap_tokens": -1}, "overlap_tokens"),
        ({"max_tokens": 16, "overlap_tokens": 16}, "overlap_tokens"),
    ],
)
def test_impossible_windows_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        chunk_text("A sentence.", tokenizer=WordTokenizer(), **kwargs)


def test_empty_text_yields_no_chunks():
    assert chunk_text("", tokenizer=WordTokenizer()) == []
    assert split_sentences(" \n\n ") == []


def test_chunking_terminates_on_pathological_input():
    text = "\n".join(["A.", "B.", "C.", "D.", "E.", "F."])
    chunks = chunk_text(text, tokenizer=CharTokenizer(), max_tokens=8, overlap_tokens=2)
    assert chunks
    assert len(chunks) < 100

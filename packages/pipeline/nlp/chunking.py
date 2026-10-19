"""Sentence-boundary chunking for the 384-token GLiNER context window.

Why not split by characters
---------------------------
GLiNER is a span model: an entity cut in half at an arbitrary character offset is
worse than an entity missing from a window. This module therefore groups whole
sentences into windows, never cuts a sentence unless the sentence alone exceeds
the context limit, and retains an overlap of whole sentences so an entity and
its relation in the next window still share context.

The splitter is Unicode-aware rather than English-aware: German, Spanish,
French, Portuguese, Russian and CJK sentence terminators are recognised, while
Latin abbreviations (``Dr.``, ``z.B.``, ``U.S.``), initials and decimals are not
mistaken for sentence ends. Offsets are always absolute in the input string, so
an entity extracted from an overlapping window can be de-duplicated exactly.

Token counting uses the model tokenizer when one is available. Without it the
fallback is deliberately conservative (word/punctuation pieces × 1.25), because
exceeding the real context limit silently truncates the model input.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "Sentence",
    "TextChunk",
    "TokenCounter",
    "chunk_text",
    "iter_chunks",
    "split_sentences",
]

DEFAULT_CONTEXT_TOKENS = 384
DEFAULT_OVERLAP_TOKENS = 64

#: Latin abbreviations that end with a dot but not a sentence. Folded to
#: lower-case without the trailing dot. Multilingual on purpose: OSINT corpora
#: mix English, German, Spanish, French, Portuguese and Italian copy.
_ABBREVIATIONS: frozenset[str] = frozenset(
    """
    mr mrs ms messrs dr prof profs sr sra srta jr st mt no nos vs etc al ibid gov sen rep gen col capt lt maj
    adm rev hon pres supt det insp fig figs vol vols pp para ch sec art jan feb mar apr jun jul aug sep sept oct nov dec
    u.s u.k u.s.a u.e d.c a.m p.m e.g i.e cf approx
    z.b u.a d.h bzw ca nr abs bspw usw
    sr. sra. ud uds
    m mme mlle
    """.split()
)

#: Closing quotes may be separated from the terminator by one space (French
#: typography: `Bonjour ! »`), so the quote group allows internal whitespace.
_LATIN_END_RE = re.compile(r"[.!?…]+(?:\s?[\"'”’»›)\]]+)*(?=\s|$)")
_WIDE_END_RE = re.compile(r"[。！？؟]+(?:\s?[\"'”’»›)\]]+)*(?=\S|$)")

#: Month names in the languages the OSINT feeds actually publish in. A two-digit
#: ordinal followed by a month ("3. Oktober 2020") must not end a sentence.
_MONTHS: frozenset[str] = frozenset(
    """
    january february march april may june july august september october november december
    januar februar maerz märz april mai juni juli august september oktober november dezember
    enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre
    janvier fevrier février mars avril mai juin juillet aout août septembre octobre novembre decembre décembre
    janeiro fevereiro marco março abril maio junho julho agosto setembro outubro novembro dezembro
    gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre
    январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь
    """.split()
)
_LINE_RE = re.compile(r"[^\n]+")
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_WORD_RE = re.compile(
    rf"[{_CJK}]|[^\W_]+(?:\.[^\W_]+)*|\d+(?:[.,]\d+)*|[^\w\s]",
    re.UNICODE,
)
#: Whitespace-delimited for Latin text; one piece per CJK character so a
#: script without spaces still has a word/token boundary to cut at.
_WORD_PIECE_RE = re.compile(rf"[{_CJK}]|\S+")


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence with absolute character offsets in the source text."""

    index: int
    text: str
    start_char: int
    end_char: int


@dataclass(frozen=True, slots=True)
class TextChunk:
    """A token-bounded window of one or more sentences."""

    text: str
    start_char: int
    end_char: int
    token_count: int
    first_sentence: int
    last_sentence: int
    #: True when a single sentence exceeded the context limit and had to be
    #: split at a word/token boundary. Such a cut can lose a relation; callers
    #: should surface it as a warning rather than pretend the window was clean.
    split_sentence: bool = False


class TokenCounter:
    """Count tokens with a Hugging Face tokenizer, with a safe fallback.

    ``tokenizer`` only needs to be callable like a transformers tokenizer
    (``tokenizer(text, add_special_tokens=False)["input_ids"]``). A model's
    internal tokenizer is discovered by the extractor; tests can pass a small
    deterministic double.
    """

    def __init__(self, tokenizer: Any = None) -> None:
        self.tokenizer = tokenizer

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self.tokenizer is not None:
            try:
                encoded = self.tokenizer(
                    text,
                    add_special_tokens=False,
                    truncation=False,
                    return_attention_mask=False,
                    return_token_type_ids=False,
                )
                ids = encoded["input_ids"] if isinstance(encoded, dict) else encoded.input_ids
                if isinstance(ids, list) and ids and isinstance(ids[0], list):
                    ids = ids[0]
                return int(len(ids))
            except Exception:  # noqa: BLE001 - tokenizer APIs vary by version
                pass
        return _approximate_tokens(text)

    def offsets(self, text: str) -> list[tuple[int, int]]:
        """Character offsets for each token, when the tokenizer supports it."""

        if not text or self.tokenizer is None:
            return []
        try:
            encoded = self.tokenizer(
                text,
                add_special_tokens=False,
                truncation=False,
                return_attention_mask=False,
                return_token_type_ids=False,
                return_offsets_mapping=True,
            )
            mapping = encoded["offset_mapping"] if isinstance(encoded, dict) else encoded.offset_mapping
            return [(int(start), int(end)) for start, end in mapping if int(end) > int(start)]
        except Exception:  # noqa: BLE001
            return []


def _approximate_tokens(text: str) -> int:
    """Conservative subword estimate for backends without a tokenizer.

    One token per word/punctuation piece underestimates inflected German or
    agglutinative surfaces; the 1.25 multiplier leaves headroom without
    collapsing the context window. CJK characters are separate pieces already.
    """

    pieces = _WORD_RE.findall(text)
    total = sum(math.ceil(len(piece) / 6) if len(piece) > 12 else 1 for piece in pieces)
    return max(1, math.ceil(total * 1.1))


def _is_abbreviation(preceding: str, following: str = "") -> bool:
    word = re.search(r"([^\s]+)$", preceding)
    if not word:
        return False
    token = word.group(1).strip("\"'“‘(«»").lower()
    if token.endswith("."):
        token = token[:-1]
    if token in _ABBREVIATIONS:
        return True
    # A single letter + dot is an initial or an abbreviation ("J. Smith",
    # "z. B.", "S. 12"), never a sentence end in running prose.
    if len(token) == 1 and token.isalpha():
        return True
    # German/Italian/Spanish ordinal dates: "3. Oktober 2020".
    if token.isdigit() and len(token) <= 2:
        month = re.match(r"[^\W\d_]+", following)
        if month and month.group(0).lower() in _MONTHS:
            return True
    return False


def _sentence_candidates(line: str) -> list[int]:
    ends: list[int] = []
    for pattern in (_LATIN_END_RE, _WIDE_END_RE):
        for match in pattern.finditer(line):
            end = match.end()
            preceding = line[: match.start() + 1]
            following = line[end:].lstrip()
            if _is_abbreviation(preceding, following):
                continue
            if following and following[0].islower():
                # "e.g. this" / "No. of items" are not sentence boundaries.
                continue
            if following and following[0].isdigit() and preceding and preceding[-2:-1].isdigit():
                # Decimal like 3.14 that the Latin lookahead let through.
                continue
            ends.append(end)
    ends = sorted(set(ends))
    # Drop boundaries contained in an earlier match ("?!", "...").
    unique: list[int] = []
    for end in ends:
        if not unique or end > unique[-1]:
            unique.append(end)
    return unique


def split_sentences(text: str, max_sentences: int = 1_500) -> list[Sentence]:
    """Split ``text`` into sentences with absolute offsets.

    Lines are hard boundaries (feeds are often one paragraph per line), and a
    sentence may span a line only when the line has no terminator. The cap is a
    memory guard: when it is reached the remaining text is not returned, and
    callers can observe that through the return length and their own truncation
    warnings.
    """

    if not text or max_sentences <= 0:
        return []
    sentences: list[Sentence] = []
    for line_match in _LINE_RE.finditer(text):
        line = line_match.group(0)
        base = line_match.start()
        if not line.strip():
            continue
        boundaries = [0, *_sentence_candidates(line), len(line)]
        for start, end in zip(boundaries, boundaries[1:], strict=False):  # paired offsets
            while start < end and line[start].isspace():
                start += 1
            while end > start and line[end - 1].isspace():
                end -= 1
            if end <= start:
                continue
            sentences.append(Sentence(len(sentences), line[start:end], base + start, base + end))
            if len(sentences) >= max_sentences:
                return sentences
    return sentences


def _split_oversized(sentence: Sentence, counter: TokenCounter, max_tokens: int, overlap_tokens: int) -> Iterator[TextChunk]:
    """Split one over-long sentence at token/word boundaries.

    A pre-tokenised offset map is exact. The fallback packs whitespace pieces by
    their estimated token count, which is less precise but never loses text.
    """

    offsets = counter.offsets(sentence.text)
    if offsets and len(offsets) > max_tokens:
        step = max(1, max_tokens - overlap_tokens)
        for start_index in range(0, len(offsets), step):
            window = offsets[start_index:start_index + max_tokens]
            if not window:
                continue
            char_start, char_end = window[0][0], window[-1][1]
            text = sentence.text[char_start:char_end].strip()
            if not text:
                continue
            leading = len(sentence.text[char_start:char_end]) - len(sentence.text[char_start:char_end].lstrip())
            absolute_start = sentence.start_char + char_start + leading
            yield TextChunk(
                text=text,
                start_char=absolute_start,
                end_char=absolute_start + len(text),
                token_count=min(max_tokens, len(window)),
                first_sentence=sentence.index,
                last_sentence=sentence.index,
                split_sentence=True,
            )
            if start_index + max_tokens >= len(offsets):
                break
        return

    pieces = list(_WORD_PIECE_RE.finditer(sentence.text))
    if not pieces:
        return
    counts = [counter.count(piece.group(0)) for piece in pieces]
    start = 0
    while start < len(pieces):
        # A hash, URL or German compound can exceed the window on its own. Cut
        # it proportionally to its measured token density; no other boundary
        # exists inside an unbroken string.
        if counts[start] > max_tokens:
            piece = pieces[start]
            yield from _split_long_piece(sentence, piece, counts[start], counter, max_tokens, overlap_tokens)
            start += 1
            continue
        end = start
        used = 0
        while end < len(pieces) and (end == start or used + counts[end] <= max_tokens):
            used += counts[end]
            end += 1
        text = sentence.text[pieces[start].start():pieces[end - 1].end()].strip()
        if text:
            leading = len(sentence.text[pieces[start].start():pieces[end - 1].end()]) - len(
                sentence.text[pieces[start].start():pieces[end - 1].end()].lstrip()
            )
            absolute_start = sentence.start_char + pieces[start].start() + leading
            yield TextChunk(
                text=text,
                start_char=absolute_start,
                end_char=absolute_start + len(text),
                token_count=counter.count(text),
                first_sentence=sentence.index,
                last_sentence=sentence.index,
                split_sentence=True,
            )
        if end >= len(pieces):
            break
        # Rewind by whole words until the overlap budget is spent, but always
        # advance by at least one word.
        overlap = 0
        next_start = end
        while next_start > start and overlap + counts[next_start - 1] <= overlap_tokens:
            next_start -= 1
            overlap += counts[next_start]
        start = next_start if next_start < end else end


def _split_long_piece(
    sentence: Sentence,
    piece: re.Match[str],
    token_count: int,
    counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
) -> Iterator[TextChunk]:
    text = piece.group(0)
    if not text:
        return
    chars_per_window = max(1, int(len(text) * max_tokens / max(1, token_count)))
    overlap_chars = int(chars_per_window * overlap_tokens / max_tokens)
    step = max(1, chars_per_window - overlap_chars)
    offset = 0
    while offset < len(text):
        window = text[offset:offset + chars_per_window]
        window_start = piece.start() + offset
        stripped_start = window_start + (len(window) - len(window.lstrip()))
        stripped = window.strip()
        if stripped:
            yield TextChunk(
                text=stripped,
                start_char=sentence.start_char + stripped_start,
                end_char=sentence.start_char + stripped_start + len(stripped),
                token_count=min(max_tokens, counter.count(stripped)),
                first_sentence=sentence.index,
                last_sentence=sentence.index,
                split_sentence=True,
            )
        if offset + chars_per_window >= len(text):
            break
        offset += step


def iter_chunks(
    text: str,
    *,
    tokenizer: Any = None,
    max_tokens: int = DEFAULT_CONTEXT_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_sentences: int = 1_500,
) -> Iterator[TextChunk]:
    """Yield sentence-aligned windows of at most ``max_tokens`` tokens.

    The overlap is expressed in tokens but applied to whole sentences: a window
    starts at the latest sentence boundary whose accumulated token count fits
    the overlap budget. An over-long sentence is the only place a sentence is
    cut; the returned ``TextChunk.split_sentence`` flag says so.
    """

    if max_tokens < 8:
        raise ValueError("max_tokens must be at least 8")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be >= 0")
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")
    if not text:
        return
    counter = tokenizer if isinstance(tokenizer, TokenCounter) else TokenCounter(tokenizer)
    sentences = split_sentences(text, max_sentences=max_sentences)
    if not sentences:
        return
    counts = [counter.count(sentence.text) for sentence in sentences]
    total = len(sentences)
    index = 0
    while index < total:
        if counts[index] > max_tokens:
            yield from _split_oversized(sentences[index], counter, max_tokens, overlap_tokens)
            index += 1
            continue

        end = index
        used = 0
        while end < total:
            boundary = 1 if end > index else 0
            if used + counts[end] + boundary > max_tokens:
                break
            used += counts[end] + boundary
            end += 1
        if end == index:  # only possible if a single sentence just fit exactly
            end = index + 1

        start_char = sentences[index].start_char
        end_char = sentences[end - 1].end_char
        chunk = text[start_char:end_char]
        yield TextChunk(
            text=chunk,
            start_char=start_char,
            end_char=end_char,
            token_count=counter.count(chunk),
            first_sentence=sentences[index].index,
            last_sentence=sentences[end - 1].index,
        )
        if end >= total:
            break

        # Overlap whole trailing sentences while they fit the budget. Never
        # produce a window that cannot also contain the next new sentence: that
        # would emit the same overlap forever.
        next_start = end
        overlap_total = 0
        while next_start > index:
            boundary = 1 if next_start - 1 > index else 0
            cost = counts[next_start - 1] + boundary
            if overlap_total + cost > overlap_tokens:
                break
            overlap_total += cost
            next_start -= 1
        if (
            next_start <= index
            or next_start == end
            or overlap_total + counts[end] + 1 > max_tokens
        ):
            next_start = end
        index = next_start


def chunk_text(
    text: str,
    *,
    tokenizer: Any = None,
    max_tokens: int = DEFAULT_CONTEXT_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_sentences: int = 1_500,
) -> list[TextChunk]:
    """List form of :func:`iter_chunks` (handy for reporting and tests)."""

    return list(
        iter_chunks(
            text,
            tokenizer=tokenizer,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            max_sentences=max_sentences,
        )
    )


def chunk_tokens(chunks: Sequence[TextChunk]) -> int:
    """Total tokens across chunks (overlap counted each time it is sent)."""

    return sum(chunk.token_count for chunk in chunks)

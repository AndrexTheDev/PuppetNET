"""spaCy-API-compatible test doubles.

``_dependency_triples`` only touches a small slice of the spaCy object model
(``token.i/text/lemma_/pos_/tag_/dep_/head/children/subtree/idx/chunk``,
``doc.text/ents/sents``, ``ent.start/end/label_/start_char/end_char/_``). The
fakes below implement exactly that surface, which lets the dependency-parsing
code path — the primary extraction route in production — be unit tested
without downloading a 500 MB model in CI.

Trees are built from a compact literal form::

    tree = make_tree(
        "Suleiman Kerimov owns Midea Holdings",
        tokens=[
            Tok("Suleiman", "PROPN", "compound", head=2),
            Tok("Kerimov",  "PROPN", "nsubj",    head=3),
            Tok("owns",     "VERB",  "ROOT",     head=3, lemma="own"),
            Tok("Midea",    "PROPN", "compound", head=5),
            Tok("Holdings", "PROPN", "dobj",     head=3),
        ],
    )
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any


class _Extensions:
    """Minimal stand-in for spaCy's ``._`` extension namespace."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = dict(values or {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._values.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_values":
            super().__setattr__(name, value)
        else:
            self._values[name] = value


class FakeToken:
    """A token with dependency links, mirroring the spaCy API subset we use."""

    def __init__(
        self,
        text: str,
        pos: str = "NOUN",
        dep: str = "dep",
        *,
        head_index: int = 0,
        lemma: str | None = None,
        tag: str | None = None,
        idx: int = 0,
        index: int = 0,
        doc: FakeDoc | None = None,
    ) -> None:
        self.text = text
        self.orth_ = text
        self.pos_ = pos
        self.dep_ = dep
        self.lemma_ = lemma if lemma is not None else text.lower()
        self.tag_ = tag or _tag_for(pos)
        self.idx = idx
        self.i = index
        self.head_index = head_index
        self.doc = doc
        self.is_space = not text.strip()
        self._ = _Extensions()
        self.lower_ = text.lower()

    # -- graph ------------------------------------------------------------
    @property
    def head(self) -> FakeToken:
        assert self.doc is not None
        return self.doc.tokens[self.head_index]

    @property
    def children(self) -> list[FakeToken]:
        assert self.doc is not None
        return [token for token in self.doc.tokens if token.head_index == self.i]

    @property
    def subtree(self) -> Iterator[FakeToken]:
        seen: set[int] = set()
        stack = [self]
        out: list[FakeToken] = []
        while stack:
            token = stack.pop()
            if token.i in seen:
                continue
            seen.add(token.i)
            out.append(token)
            stack.extend(token.children)
        out.sort(key=lambda t: t.i)
        yield from out

    @property
    def n_lefts(self) -> int:
        return len(self.children)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeToken {self.text!r} {self.dep_} → {self.head_index}>"


def _tag_for(pos: str) -> str:
    return {
        "VERB": "VBD",
        "AUX": "VBD",
        "PROPN": "NNP",
        "NOUN": "NN",
        "ADP": "IN",
        "DET": "DT",
        "ADJ": "JJ",
        "PUNCT": ".",
        "CCONJ": "CC",
        "PART": "RB",
        "ADV": "RB",
    }.get(pos.upper(), "NN")


class FakeSpan:
    """Entity span over a :class:`FakeDoc`."""

    def __init__(self, doc: FakeDoc, start: int, end: int, label: str, *, craft_props: dict[str, Any] | None = None) -> None:
        self.doc = doc
        self.start = start
        self.end = end
        self.label_ = label
        self._ = _Extensions({"craft_props": craft_props})

    @property
    def text(self) -> str:
        return " ".join(token.text for token in self.doc.tokens[self.start:self.end])

    @property
    def start_char(self) -> int:
        return self.doc.tokens[self.start].idx

    @property
    def end_char(self) -> int:
        last = self.doc.tokens[self.end - 1]
        return last.idx + len(last.text)

    @property
    def root(self) -> FakeToken:
        return self.doc.tokens[self.start]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FakeSpan {self.text!r} {self.label_}>"


class FakeSentence:
    """A sentence slice of a :class:`FakeDoc`."""

    def __init__(self, doc: FakeDoc, start: int, end: int) -> None:
        self.doc = doc
        self.start = start
        self.end = end

    def __iter__(self) -> Iterator[FakeToken]:
        return iter(self.doc.tokens[self.start:self.end])

    def __len__(self) -> int:
        return self.end - self.start

    def __getitem__(self, item: int) -> FakeToken:
        return self.doc.tokens[self.start + item]

    @property
    def text(self) -> str:
        return self.doc.text[self.doc.tokens[self.start].idx:self.doc.tokens[self.end - 1].idx + len(self.doc.tokens[self.end - 1].text)]

    @property
    def start_char(self) -> int:
        return self.doc.tokens[self.start].idx


class FakeDoc:
    """Document double: token list, entity spans and sentence boundaries."""

    def __init__(self, tokens: Sequence[FakeToken], sentence_bounds: Sequence[int] = ()) -> None:
        self.tokens = list(tokens)
        for index, token in enumerate(self.tokens):
            token.i = index
            token.doc = self
        self._sentence_bounds = list(sentence_bounds) or [len(self.tokens)]
        self._ = _Extensions()

    # -- text -------------------------------------------------------------
    @property
    def text(self) -> str:
        out: list[str] = []
        cursor = 0
        for token in self.tokens:
            if token.idx > cursor:
                out.append(" " * (token.idx - cursor))
            out.append(token.text)
            cursor = token.idx + len(token.text)
        return "".join(out)

    def __iter__(self) -> Iterator[FakeToken]:
        return iter(self.tokens)

    def __len__(self) -> int:
        return len(self.tokens)

    def __getitem__(self, item: Any) -> Any:
        return self.tokens[item]

    # -- spans ------------------------------------------------------------
    @property
    def ents(self) -> tuple[FakeSpan, ...]:
        return tuple(getattr(self, "_ents", ()))

    def set_ents(self, ents: Sequence[FakeSpan], default: str = "unmodified") -> None:
        self._ents = tuple(ents)

    @property
    def sents(self) -> Iterator[FakeSentence]:
        start = 0
        for end in self._sentence_bounds:
            yield FakeSentence(self, start, end)
            start = end


@dataclass
class Tok:
    """Compact token literal used by :func:`make_tree`."""

    text: str
    pos: str = "NOUN"
    dep: str = "dep"
    head: int = 0
    lemma: str | None = None
    tag: str | None = None


def make_tree(text: str, tokens: Sequence[Tok], *, ents: Sequence[tuple[int, int, str]] = (), sentence_bounds: Sequence[int] = ()) -> FakeDoc:
    """Build a :class:`FakeDoc` with character offsets derived from ``text``."""
    fake_tokens: list[FakeToken] = []
    cursor = 0
    for spec in tokens:
        index = text.find(spec.text, cursor)
        if index < 0:  # pragma: no cover - test authoring error
            raise ValueError(f"token {spec.text!r} not found in {text!r} at/after offset {cursor}")
        fake_tokens.append(
            FakeToken(
                spec.text,
                spec.pos,
                spec.dep,
                head_index=spec.head,
                lemma=spec.lemma,
                tag=spec.tag,
                idx=index,
            )
        )
        cursor = index + len(spec.text)
    doc = FakeDoc(fake_tokens, sentence_bounds=sentence_bounds or [len(fake_tokens)])
    doc.set_ents([FakeSpan(doc, start, end, label) for start, end, label in ents])
    return doc

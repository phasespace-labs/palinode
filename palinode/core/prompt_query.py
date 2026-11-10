"""Remove harness output and conversational boilerplate from automatic recall."""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

_HARNESS = (
    "local-command-caveat|command-name|command-args|bash-input|bash-output|"
    "pasted_content|system-reminder|task-notification"
)
_BLOCK = re.compile(rf"<({_HARNESS})\b[^>]*>(?:(?!<\1\b).)*?</\1\s*>", re.I | re.S)
_SMALLTALK = re.compile(
    r"^(?:thanks?\b|thank you\b|good morning\b|hello\b|hi\b|all done\b)", re.I,
)
_QUESTION_FRAME = re.compile(r"^(?:what|which)\b.*\b(?:do|does|did)\b", re.I)
_GENERIC_END = re.compile(r"\s+(?:use|serve)\s*\??$", re.I)
_QUESTION_WORDS = frozenset(
    "what which who whom whose how when where why is are was were do does did can could will "
    "would should shall may might must have has had".split())
MAX_PROMPT_QUERY_CHARS = 1000


def shape_prompt(query: str) -> str:
    """Keep user questions; omit local output and small talk.

    This is deterministic query preparation, not intent inference by a model.
    A command containing a question still keeps the question. Commands are
    kept otherwise: "fix the watcher debounce" is exactly the turn where a
    recorded decision about the watcher matters, and the confident-only
    delivery gate, not query preparation, is what silences a command with no
    relevant record. Discarded markup never reaches embedding.
    """
    text = query
    # Repeat for nested harness blocks; unclosed blocks consume the remainder.
    while True:
        stripped = _BLOCK.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(rf"<({_HARNESS})\b[^>]*>.*$", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    questions = [s for s in sentences if "?" in s]
    kept = questions or [s for s in sentences if not _SMALLTALK.match(s)]
    # Generic question verbs can dominate IDF when a small store uses the
    # inflected form ("uses", "serves"). The subject supplies the topic.
    kept = [_GENERIC_END.sub("", s) if _QUESTION_FRAME.match(s) else s for s in kept]
    return " ".join(kept)[:MAX_PROMPT_QUERY_CHARS].strip()


#: Words that open a command without being its verb ("ok now", "please",
#: "lets", "can you", "go ahead and").
_COMMAND_FILLER = frozenset("""
ok okay now so please lets let's then alright sure yes go ahead just also again can could would
you quickly
""".split())
#: Function words the FTS stopword list leaves out but a parse must skip.
_FUNCTION_EXTRA = frozenset("onto via per across within upon toward towards".split())
#: "do" opens a question ("do you…") or a command ("do the same for…").
_SUBJECT_PRONOUNS = frozenset("i you we they he she".split())
_CLAUSE_SPLIT = re.compile(r"\s*(?:[,;:]|\band\b|\bthen\b)\s*", re.I)
_WORD = re.compile(r"[#\w][\w.\-/#]*")
_TRACKER_REF = re.compile(r"#?\d+$")
_IDENTIFIER = re.compile(r".*(?:[A-Za-z]\d|\d[A-Za-z]|[._/]|[a-z][A-Z]).*")


def _load_working_state() -> frozenset[str]:
    text = resources.files("palinode.core").joinpath("working_state.txt").read_text(encoding="utf-8")
    return frozenset(
        word.lower()
        for line in text.splitlines()
        for word in line.split("#", 1)[0].split()
    )


#: The working copy and the session: objects every command can name and no
#: stored record is about, plus words pointing back into the conversation.
#: Shipped as data (``working_state.txt``) so the list is reviewable on its own.
WORKING_STATE: frozenset[str] = _load_working_state()


@dataclass(frozen=True)
class PromptShape:
    """What a prompt asks about, parsed deterministically.

    ``query`` is :func:`shape_prompt`'s text, the string retrieval runs on.
    ``subject_terms`` are the words that could name something a memory is
    about; ``working_state_terms`` are the prompt's words from
    :data:`WORKING_STATE`. ``command`` is true when the prompt opens with an
    imperative clause; ``question`` when it holds a question mark or opens
    with a question word. A command clause's first word is its verb, and is
    neither kind of term.
    """

    query: str
    subject_terms: tuple[str, ...]
    working_state_terms: tuple[str, ...]
    question: bool
    command: bool

    @property
    def subject_units(self) -> frozenset[str]:
        """The subject terms as the keyword arm's match units, lowercased."""
        from palinode.core.store import fts_match_units

        return frozenset(
            unit.lower() for term in self.subject_terms for unit in fts_match_units(term)
        )


def parse_prompt(shaped: str) -> PromptShape:
    """Split a shaped prompt into subject and working-state terms.

    Clauses split on punctuation, "and" and "then". In a prompt that opens
    with a command, each clause's first word (after filler) is a verb, as is
    the word after "to"; a later clause opened by a pronoun ("…, it broke the
    dashboard") is a statement and keeps its first word. A prompt that opens
    any other way (a question word, a pronoun) keeps every word. What is left,
    minus stopwords, filler and tracker numbers, is subject unless it is in
    :data:`WORKING_STATE`. An identifier (``sync.batch_window_ms``,
    ``ADR-012``) is always subject.
    """
    from palinode.core.store import FTS_STOPWORDS

    question = "?" in shaped
    command = False
    subject: list[str] = []
    working: list[str] = []
    decided = False
    for clause in _CLAUSE_SPLIT.split(shaped) if shaped else ():
        words = _WORD.findall(clause)
        while words and words[0].lower() in _COMMAND_FILLER:
            words = words[1:]
        if not words:
            continue
        head = words[0]
        imperative_do = head.lower() == "do" and (
            len(words) == 1 or words[1].lower() not in _SUBJECT_PRONOUNS)
        opens_statement = not imperative_do and (
            head.lower() in FTS_STOPWORDS or bool(_IDENTIFIER.match(head))
            or bool(_TRACKER_REF.match(head)))
        if not decided:
            # The first clause with words decides what kind of prompt this is.
            decided = True
            question = question or (opens_statement and head.lower() in _QUESTION_WORDS)
            command = not opens_statement and "?" not in shaped
        if command and not opens_statement:
            rest = words[1:]
        else:
            rest = words
        skip_next = False
        for word in rest:
            low = word.lower()
            if command and skip_next:  # the verb of a "to <verb>" infinitive
                skip_next = False
                continue
            if command and low == "to":
                skip_next = True
                continue
            if _IDENTIFIER.match(word):
                subject.append(word)
            elif low in WORKING_STATE:
                working.append(low)
            elif (low in FTS_STOPWORDS or low in _COMMAND_FILLER or low in _FUNCTION_EXTRA
                    or _TRACKER_REF.match(word)):
                continue
            else:
                subject.append(word)
    return PromptShape(
        query=shaped,
        subject_terms=tuple(dict.fromkeys(subject)),
        working_state_terms=tuple(dict.fromkeys(working)),
        question=question,
        command=command,
    )


def prompt_shape(query: str) -> PromptShape:
    """:func:`shape_prompt`, returning the typed parse beside the text."""
    return parse_prompt(shape_prompt(query))


def command_without_subject(shaped: str) -> bool:
    """A command naming nothing the store could hold ("bump the version and tag it")."""
    shape = parse_prompt(shaped)
    return shape.command and not shape.subject_terms

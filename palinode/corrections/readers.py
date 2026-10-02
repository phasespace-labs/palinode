"""Per-harness transcript readers.

One small interface, one implementation. Claude Code is the only harness read in
this release; Codex CLI is the named follow-up, and it gets its own reader rather
than a general transcript framework — the two formats share nothing but the fact
that they are append-only logs.

Everything here is read-only and bounded. The reader never writes to a harness
directory, opens every file ``O_RDONLY`` without following a symlink, and stops
at the per-file line and byte ceilings rather than pulling an arbitrarily large
session into memory.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

logger = logging.getLogger("palinode.corrections.readers")

#: Lines read from one transcript before the reader stops. A session far longer
#: than this exists (long agent runs append thousands of bookkeeping lines), and
#: the ceiling is reported, never silently applied.
MAX_LINES_PER_TRANSCRIPT = 20_000

#: A single line larger than this is skipped. One pasted file can be megabytes,
#: and no candidate span is ever going to come from the user's own typing at
#: that size.
MAX_LINE_BYTES = 1_000_000

#: ``promptSource`` values that mean the turn was submitted by something other
#: than the person: a harness-generated prompt or an SDK caller. Absent is the
#: common case for a typed turn and is treated as the person typing.
_NON_HUMAN_PROMPT_SOURCES = frozenset({"system", "sdk"})


class TranscriptPathError(ValueError):
    """A configured transcript path is not safe to read."""


@dataclass(frozen=True)
class TranscriptTurn:
    """One message-bearing turn, with the provenance a candidate needs.

    ``is_user_evidence`` is the whole point of the type: a correction is only
    evidence when the *user* typed it. Tool output replayed on a ``user`` line,
    a compact summary, injected metadata and a subagent thread all look like
    user turns in the raw format and none of them are the user speaking.
    """

    harness: str
    session_id: str
    turn_index: int
    turn_uuid: str | None
    role: str
    text: str
    timestamp: str | None
    cwd: str | None
    git_branch: str | None
    is_user_evidence: bool
    ineligible_reason: str | None = None


@dataclass(frozen=True)
class TranscriptSession:
    """One transcript file's turns, in order."""

    harness: str
    session_id: str
    path: Path
    cwd: str | None
    turns: tuple[TranscriptTurn, ...] = field(default_factory=tuple)
    truncated: bool = False


class TranscriptReader(Protocol):
    """The per-harness contract: name yourself, and yield sessions from a root."""

    harness: str

    def sessions(self, root: Path) -> Iterator[TranscriptSession]:
        ...


def validate_transcript_root(raw: str) -> Path:
    """Return a safe, absolute transcript root, or raise.

    A transcript root is operator-supplied configuration pointing *outside* the
    memory store, so the store's own path guard does not apply. The rules are
    the ones that matter for a directory we are about to walk: absolute after
    ``~`` expansion, no ``..`` segment, and no symlink anywhere on the way down
    — a symlinked component is exactly how a configured "read my Claude Code
    projects" turns into "read anything on this disk".
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise TranscriptPathError("Invalid transcript path")
    expanded = Path(os.path.expanduser(raw.strip()))
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise TranscriptPathError(f"Transcript path must be absolute and free of '..': {raw!r}")
    probe = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        probe = probe / part
        if probe.is_symlink():
            raise TranscriptPathError(f"Transcript path traverses a symlink: {raw!r}")
    return expanded


def _open_readonly(path: Path) -> Iterator[str]:
    """Yield the lines of *path*, opened read-only and without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def _blocks_text(content: Any) -> str:
    """Join the ``text`` blocks of a message; ignore every other block type.

    Thinking blocks are the model's, tool-use blocks are arguments and
    tool-result blocks are output. None of them is the user's typing, so none of
    them is evidence, and concatenating them would put tool output inside a span.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def _evidence_state(record: dict[str, Any], role: str, text: str) -> tuple[bool, str | None]:
    """Decide whether this turn is the user's own words, and say why not.

    The *reason* is load-bearing beyond detection: the classifier's window uses
    it to decide what a neighbouring turn may contribute (see
    ``palinode.corrections.classify``), and "an assistant reply" and "a
    subagent's tool output" must not collapse into one answer. That is why the
    structural flags are tested before the role — a sidechain assistant turn is
    a sidechain, not merely "not the user".
    """
    if record.get("toolUseResult") is not None:
        return False, "tool_output"
    if record.get("isSidechain") is True:
        return False, "sidechain"
    if role != "user":
        return False, "not_a_user_turn"
    if record.get("isMeta") is True:
        return False, "meta"
    if record.get("isCompactSummary") is True:
        return False, "compact_summary"
    prompt_source = record.get("promptSource")
    if isinstance(prompt_source, str) and prompt_source in _NON_HUMAN_PROMPT_SOURCES:
        return False, f"prompt_source_{prompt_source}"
    if not text.strip():
        return False, "empty"
    return True, None


class ClaudeCodeTranscriptReader:
    """Reader for Claude Code's ``<root>/<project-slug>/<session>.jsonl`` logs."""

    harness = "claude-code"

    def sessions(self, root: Path) -> Iterator[TranscriptSession]:
        """Yield one :class:`TranscriptSession` per readable transcript under *root*."""
        if not root.is_dir():
            logger.info(
                "transcript root is not a directory, skipping harness=%s root=%s",
                self.harness, root,
            )
            return
        for path in sorted(root.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                logger.info("skipping non-regular transcript path=%s", path)
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                logger.info("skipping transcript outside its configured root path=%s", path)
                continue
            session = self.read_session(path)
            if session is not None:
                yield session

    def read_session(self, path: Path) -> TranscriptSession | None:
        """Parse one transcript file into ordered turns, or ``None`` if unreadable."""
        turns: list[TranscriptTurn] = []
        session_id = ""
        cwd: str | None = None
        truncated = False
        turn_index = -1
        try:
            for line_number, line in enumerate(_open_readonly(path)):
                if line_number >= MAX_LINES_PER_TRANSCRIPT:
                    truncated = True
                    logger.warning(
                        "transcript line ceiling reached; remaining lines not read "
                        "path=%s ceiling=%d", path, MAX_LINES_PER_TRANSCRIPT,
                    )
                    break
                if len(line) > MAX_LINE_BYTES:
                    truncated = True
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if isinstance(record.get("sessionId"), str) and not session_id:
                    session_id = record["sessionId"]
                if record.get("type") not in {"user", "assistant"}:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in {"user", "assistant"}:
                    continue
                if cwd is None and isinstance(record.get("cwd"), str):
                    cwd = record["cwd"]
                text = _blocks_text(message.get("content"))
                is_evidence, reason = _evidence_state(record, role, text)
                turn_index += 1
                turns.append(
                    TranscriptTurn(
                        harness=self.harness,
                        session_id=session_id or path.stem,
                        turn_index=turn_index,
                        turn_uuid=record.get("uuid") if isinstance(record.get("uuid"), str) else None,
                        role=role,
                        text=text,
                        timestamp=record.get("timestamp") if isinstance(record.get("timestamp"), str) else None,
                        cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                        git_branch=record.get("gitBranch") if isinstance(record.get("gitBranch"), str) else None,
                        is_user_evidence=is_evidence,
                        ineligible_reason=reason,
                    )
                )
        except OSError as error:
            logger.warning("could not read transcript path=%s error=%r", path, error)
            return None
        return TranscriptSession(
            harness=self.harness,
            session_id=session_id or path.stem,
            path=path,
            cwd=cwd,
            turns=tuple(turns),
            truncated=truncated,
        )


#: Every harness this release can read. A second entry is a second reader, not a
#: generalisation of this one.
_READERS: dict[str, TranscriptReader] = {
    ClaudeCodeTranscriptReader.harness: ClaudeCodeTranscriptReader(),
}


def reader_for_harness(harness: str) -> TranscriptReader | None:
    """Return the reader for *harness*, or ``None`` when it is not supported."""
    return _READERS.get(harness)


def supported_harnesses() -> tuple[str, ...]:
    return tuple(sorted(_READERS))


__all__ = [
    "ClaudeCodeTranscriptReader",
    "MAX_LINES_PER_TRANSCRIPT",
    "MAX_LINE_BYTES",
    "TranscriptPathError",
    "TranscriptReader",
    "TranscriptSession",
    "TranscriptTurn",
    "reader_for_harness",
    "supported_harnesses",
    "validate_transcript_root",
]

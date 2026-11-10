"""Tool-envelope markup guard for write-path string parameters.

A string parameter on a write surface should carry *content*, never the *tool
envelope* that delivered it. Two entry points put an envelope there:

  1. a malformed model tool-call, whose tail is absorbed into the preceding
     string parameter — which also swallows any parameters that followed it;
  2. a hook's transcript extraction lifting harness markup straight out of a
     transcript turn (fixed at source in examples/hooks/palinode-session-end.sh;
     this is the backstop).

Detection deliberately is NOT a substring blacklist. Palinode is a memory system
for developers, and a note *about* tool-call syntax is legitimate content — the
investigation that produced this guard has to stay saveable. A fragment from the
vocabulary below is only a *candidate*; rejection needs a corroborating signal,
and anything inside a code fence or code span is exempt outright.

Three signals, strongest first:

  1. **Co-occurrence** — the parameters that should have arrived alongside this
     string didn't. That is the mechanism-1 absorption signature and the only
     near-zero-false-positive signal, but it is available *only* to a caller
     with parameters that are always expected. Callers declare it by naming
     them in ``missing_params``.
  2. **Structural invalidity** — a closing tag with no unclosed opener of the
     same name before it.
  3. **Positional** — a fragment at the very tail of the value, where an
     absorbed envelope lands.

Signal 1 does not generalise, and assuming it does is the trap this module
exists to make visible. ``session_end`` has it: the /wrap flow essentially
always sends ``decisions``/``blockers``, so their absence is genuinely
anomalous. ``save`` does not: every one of its array parameters is optional and
absent on the majority of honest calls, so "no arrays arrived" is the norm
there, not a signature. A caller without the signal passes ``missing_params=()``
and is guarded by signals 2 and 3 alone — narrower on purpose, because the
alternative is a blanket ban on the vocabulary, which would reject a
``<details><summary>…</summary></details>`` block in an ordinary markdown note.
"""
from __future__ import annotations

import re

#: Tag names whose appearance in a write-path string is a candidate envelope
#: leak. Three groups: model tool-call syntax, write-surface parameter names
#: (what mechanism-1 absorption leaves behind), and Claude Code harness markup.
#:
#: One vocabulary serves every surface; the per-surface discrimination lives in
#: the signals, not in the word list. Parameter names that are also ordinary
#: markup are deliberately absent: ``<project>`` is the root element of real
#: build files, and ``<title>``/``<content>``/``<type>``/``<metadata>`` are
#: common enough in notes *about* XML that including them would cost more in
#: false positives than the tail/unmatched signals recover.
ENVELOPE_TAGS: tuple[str, ...] = (
    "invoke", "parameter", "function_calls", "tool_call", "tool_use",
    "summary", "decisions", "blockers",
    "system-reminder", "command-message", "command-name", "command-args",
    "local-command-stdout", "local-command-stderr", "user-prompt-submit-hook",
    "bash-input", "bash-stdout", "bash-stderr",
    "ide_selection", "ide_opened_file",
)

#: Matches `<tag>`, `</tag>`, `<tag …attrs>` and namespaced forms (`<invoke>`).
#: Group 1 = "/" for a closing tag, group 2 = the bare tag name.
_ENVELOPE_RE = re.compile(
    r"<(/?)(?:[A-Za-z][\w.-]*:)?(" + "|".join(ENVELOPE_TAGS) + r")(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)

#: Fenced blocks and inline spans are the escape hatch: markup quoted as code is
#: always content. Replaced with a space so surrounding offsets stay meaningful.
_CODE_SPANS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"~~~.*?~~~", re.DOTALL),
    re.compile(r"`[^`\n]+`"),
)


def strip_code(text: str) -> str:
    for pattern in _CODE_SPANS:
        text = pattern.sub(" ", text)
    return text


def envelope_complaint(
    text: str,
    field: str,
    *,
    missing_params: tuple[str, ...] = (),
    remediation: str = "",
) -> str | None:
    """Return an actionable rejection message when ``text`` carries a tool
    envelope rather than content, else ``None``.

    Args:
        text: the string parameter's value.
        field: its name, quoted back to the caller in the message.
        missing_params: names of parameters that were expected on *this* request
            and did not arrive. Non-empty enables signal 1 (co-occurrence), under
            which any candidate fragment is enough to reject. Pass ``()`` — the
            default — when the caller has no parameter whose absence is
            anomalous; the guard then relies on signals 2 and 3 only. See the
            module docstring for why this does not generalise across surfaces.
        remediation: an optional surface-specific "here is how to re-send"
            sentence, placed before the fenced-code escape hatch.
    """
    scrubbed = strip_code(text)
    matches = list(_ENVELOPE_RE.finditer(scrubbed))
    if not matches:
        return None

    # A closer is matched only by an opener of the same name that comes before
    # it and is still open. Order matters: in `…</parameter>\n<parameter
    # name="decisions">[…]` the opener follows the closer, and a name-keyed set
    # once counted that closer as matched. Self-closing tags open nothing.
    open_counts: dict[str, int] = {}
    unmatched = None
    for m in matches:
        name = m.group(2).lower()
        if m.group(1):
            if open_counts.get(name, 0) == 0:
                unmatched = m
                break
            open_counts[name] -= 1
        elif not m.group(0).endswith("/>"):
            open_counts[name] = open_counts.get(name, 0) + 1
    # Absorption lands the envelope at the very tail of the value.
    tail_end = len(scrubbed.rstrip())
    trailing = next((m for m in matches if m.end() == tail_end), None)

    if missing_params:
        named = "/".join(f"`{p}`" for p in missing_params)
        # State the observation, not the mechanism. Absorption is the *reason*
        # this signal exists, but the server cannot distinguish "the parameter
        # was destroyed in transit" from "the caller did not send one" — both
        # arrive as absence. Naming absorption here read as a finding rather
        # than a hypothesis, and readers relayed it back as their diagnosis:
        # twice now an investigation has concluded "the tool envelope broke"
        # on no evidence beyond this sentence, and gone looking in the
        # transport while the real cause sat in the payload. An error message
        # is read far more often than the code that emits it, and whatever
        # explanation it offers is the one that ends up in the issue tracker.
        offender, why = (unmatched or trailing or matches[-1]), (
            f"and no {named} arrived with it"
        )
        cause = (
            f"Two things look identical from here and only you can tell them "
            f"apart: a malformed tool call whose tail was absorbed into "
            f"`{field}`, swallowing the parameters that followed it — or an "
            f"ordinary call that quoted markup and simply did not send "
            f"{named}."
        )
    elif unmatched is not None:
        offender, why = unmatched, "as a closing tag with no matching opener"
        cause = ""
    elif trailing is not None:
        offender, why = trailing, "at the very end of the value, where an absorbed envelope lands"
        cause = ""
    else:
        return None

    sentences = [
        # The offender is rendered in backticks, not repr. A rejection message
        # is the thing a caller most wants to quote — into a retry summary, an
        # issue, a session note — and repr's bare quotes leave the fragment
        # unprotected, so quoting the message re-trips the guard and the retry
        # fails identically. Six consecutive failures were once diagnosed as a
        # malformed tool call for exactly this reason: the loop is
        # indistinguishable from a deterministic transport fault. Backticks are
        # already the documented escape hatch, so a message that uses them is
        # safe to paste back verbatim.
        f"Refusing to store `{field}`: it contains tool-envelope markup "
        f"{why} — `{offender.group(0)}`.",
        "Palinode fails loud here rather than indexing an envelope as if it "
        "were memory.",
    ]
    if cause:
        sentences.append(cause)
    if remediation:
        sentences.append(remediation)
    sentences.append(
        "If the markup really is part of the note, wrap it in backticks on a "
        "single line, or put it in a fenced code block, and it will pass."
    )
    return " ".join(sentences)

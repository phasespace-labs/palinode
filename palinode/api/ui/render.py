"""Markdown rendering + HTML sanitization for the provenance UI.

Store-agnostic on purpose: these helpers take a markdown string and return
sanitized HTML. They know nothing about palinode's store, config, or request
objects, so ``weir`` (or any other consumer rendering agent-generated content)
can import them directly.

Security note: this is an audit tool rendering *agent-generated* content. Raw
HTML in a memory body is an XSS vector. We render markdown with
``markdown-it-py`` (HTML disabled) and then run the output through ``nh3``
(an ammonia/html5ever sanitizer) as a defense-in-depth backstop. Both layers
are mandatory — the sanitizer is the load-bearing one because it strips any
``<script>``/``onerror=``/``javascript:`` payload regardless of how it entered
the markdown.
"""
from __future__ import annotations

import nh3
from markdown_it import MarkdownIt

from palinode.core.memory_write import _WIKI_FOOTER_MARKER

# Tags we allow through the sanitizer. Deliberately conservative: prose,
# headings, lists, code, tables, links. No <script>, <style>, <iframe>,
# <object>, <form>, or event-handler-bearing elements.
_ALLOWED_TAGS: set[str] = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "ins", "mark",
    "blockquote", "code", "pre", "kbd", "samp",
    "ul", "ol", "li",
    "a",
    "table", "thead", "tbody", "tr", "th", "td",
    "span", "div",
}

# Per-tag allowed attributes. Anchors get href/title/rel; code/span carry a
# class for highlight styling. No ``style`` attribute anywhere (CSS injection),
# no event handlers.
_ALLOWED_ATTRS: dict[str, set[str]] = {
    # ``rel`` is managed by nh3 via ``link_rel`` below — must not also be in
    # the allow-list (nh3 rejects the combination).
    "a": {"href", "title"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "td": {"align"},
    "th": {"align"},
}

# Only these URL schemes survive on links. ``javascript:`` and ``data:`` are
# rejected (nh3 strips the whole attribute when the scheme is not allowed).
_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto"}

# Configure markdown-it with HTML rendering DISABLED. Even before nh3 runs,
# inline/raw HTML in the source is escaped rather than passed through.
_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
_md.enable("table")

# The wiki footer's marker comment sits on the line after its "## See also"
# heading. With HTML off, markdown-it escapes it and the page shows it as
# text. It is dropped from markdown-it's own token stream, so fences, indented
# code, lists and quotes are already parsed: only a top-level paragraph whose
# whole text is the marker, directly after a top-level h2 "See also", goes.
# The marker in any other place (code, a quote, a list, prose) still shows.
def _drop_footer_marker(tokens: list) -> list:
    kept: list = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.type == "paragraph_open"
            and tok.level == 0
            and i + 2 < len(tokens)
            and tokens[i + 1].type == "inline"
            and tokens[i + 1].content.strip() == _WIKI_FOOTER_MARKER
            and tokens[i + 2].type == "paragraph_close"
            and len(kept) >= 3
            and kept[-1].type == "heading_close"
            and kept[-3].type == "heading_open"
            and kept[-3].tag == "h2"
            and kept[-3].level == 0
            and kept[-2].type == "inline"
            and kept[-2].content.strip() == "See also"
        ):
            i += 3
            continue
        kept.append(tok)
        i += 1
    return kept


def render_markdown(body: str) -> str:
    """Render a markdown *body* to sanitized HTML.

    1. ``markdown-it-py`` with ``html=False`` (raw HTML is escaped at parse).
    2. ``nh3`` sanitize against the allow-lists above (defense in depth).

    Returns a safe HTML fragment suitable for direct insertion into a template
    with the ``| safe`` filter. Never raises on malformed markdown; an empty or
    whitespace-only body returns ``""``.
    """
    if not body or not body.strip():
        return ""
    env: dict = {}
    tokens = _drop_footer_marker(_md.parse(body, env))
    raw_html = _md.renderer.render(tokens, _md.options, env)
    return sanitize_html(raw_html)


def sanitize_html(html: str) -> str:
    """Sanitize an HTML fragment with ``nh3`` against the UI allow-lists.

    Exposed separately so callers that already have HTML (not markdown) can
    reuse the same policy. Strips disallowed tags/attributes/URL schemes;
    adds ``rel="noopener noreferrer"`` to links and forces external links to
    a new tab is intentionally NOT done here (read-only audit context, links
    are informational).
    """
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes={tag: set(attrs) for tag, attrs in _ALLOWED_ATTRS.items()},
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )

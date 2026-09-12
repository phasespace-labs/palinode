"""Write-time forgetting: "please forget X" → archival with a tombstone.

Covers the deterministic detector (the dataset's explicit request forms fire;
assistant acknowledgements, negations and self-reports must not), phrase
extraction, and the save-path hook end to end: the resolved pref memory leaves
default recall, the forget-request memory stays retrievable (the measured
load-bearing part), unrelated
memories are untouched, and the hook is off by default.

Real SQLite + real git + real FTS5 in ``tmp_path``, no DB mocking (repo rule).
Only the embedder and the security scanner are patched; the embedder fake is
bag-of-words-hash based so token overlap yields genuine vector similarity and
hybrid ranking stays meaningful.
"""
from __future__ import annotations

import hashlib
import importlib
import math
import os
import subprocess
from unittest.mock import patch

import frontmatter
import pytest

from palinode.consolidation import forget
from palinode.consolidation.forget import detect_forget_request
from palinode.core.config import config
from palinode.core.embedder import EmbeddingUnavailable

EMBED_DIM = 1024


def _hash_embed(text: str, backend: str = "local") -> list[float]:
    """Deterministic bag-of-words embedding: shared tokens → shared components,
    so cosine similarity tracks token overlap instead of being uniform."""
    vec = [0.0] * EMBED_DIM
    for tok in set(text.lower().split()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── detector ──────────────────────────────────────────────────────────────────

# The PersonaMem-v2 request forms, verbatim shapes from the benchmark data.
@pytest.mark.parametrize("text,expected", [
    ("Please forget that I frequently re-watch favorite childhood movies.",
     "I frequently re-watch favorite childhood movies"),
    ("Please forget my preference about visiting art exhibitions featuring "
     "contemporary Asian artists.",
     "my preference about visiting art exhibitions featuring contemporary "
     "Asian artists"),
    ("Please forget the detail about my friendship ending over political "
     "differences and the resulting trust issues.",
     "my friendship ending over political differences and the resulting "
     "trust issues"),
    ("Please forget that I’m managing a vitamin D deficiency with daily "
     "supplements.",
     "I’m managing a vitamin D deficiency with daily supplements"),
    ("Please forget the detail about my family estrangement during my "
     "transition process from your memory.",
     "my family estrangement during my transition process"),
    ("Please forget the detail about me mediating a company layoff involving "
     "colleagues I cared for.",
     "me mediating a company layoff involving colleagues I cared for"),
    ("Could you forget that I wear glasses for myopia?",
     "I wear glasses for myopia"),
    ("I'd like you to forget that I collect sneakers.",
     "I collect sneakers"),
    ("Forget the detail about my old apartment in Berlin.",
     "my old apartment in Berlin"),
])
def test_detector_fires_on_explicit_requests(text, expected):
    assert detect_forget_request(text) == expected


def test_detector_cuts_at_sentence_boundary():
    text = ("Please forget that I enjoy fresh fruit smoothies. Now, what are "
            "some refreshing breakfast options?")
    assert detect_forget_request(text) == "I enjoy fresh fruit smoothies"


# Assistant acknowledgements repeat the pref with a second-person object; a
# detector firing on them would archive on the echo, not the request.
@pytest.mark.parametrize("text", [
    "Got it — I’ll forget that detail about you frequently re-watching "
    "favorite childhood movies.",
    "Got it — I’ll forget that you enjoy fresh fruit smoothies.",
    "Got it — I’ll forget that preference.",
    "Got it — I’ll forget your earlier preference for visiting art "
    "exhibitions featuring contemporary Asian artists.",
    "Alright — I’ll forget that detail. It won’t be included in my responses "
    "going forward.",
    "I’ll no longer take into account that you have a family history of "
    "type 2 diabetes.",
])
def test_detector_ignores_assistant_acks(text):
    assert detect_forget_request(text) is None


@pytest.mark.parametrize("text", [
    # Negation: the opposite instruction.
    "Please don't forget that I have a meeting tomorrow.",
    "Don't forget that I collect sneakers.",
    "Could you not forget that I prefer window seats?",
    "Never forget that I love hiking.",
    # Self-report about forgetting, not a request.
    "I always forget my keys when I leave in a hurry.",
    "Sometimes I forget that I already answered this.",
    # No forget verb at all.
    "Please recall my related preferences from our conversation history.",
    "",
])
def test_detector_ignores_non_requests(text):
    assert detect_forget_request(text) is None


# ── save-path hook, end to end ────────────────────────────────────────────────


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient over a git-backed tmp memory_dir with hash-embed vectors."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email",
                    "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name",
                    "test"], check=True)

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", True)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    from fastapi.testclient import TestClient
    with (
        patch("palinode.core.store.scan_memory_content",
              return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", side_effect=_hash_embed),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c, str(tmp_path)
    srv._rate_counters.clear()


def _save(client, content: str, slug: str) -> dict:
    r = client.post("/save", json={
        "content": content, "type": "Insight", "slug": slug,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _search_paths(client, query: str) -> list[str]:
    r = client.post("/search", json={"query": query, "limit": 10,
                                     "threshold": 0.0, "hybrid": True})
    assert r.status_code == 200, r.text
    return [os.path.basename(h["file_path"]) for h in r.json()]


def test_forget_hook_archives_pref_and_keeps_request(api_client, monkeypatch):
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    _save(client, "Prefers green tea over coffee in the mornings.",
          "pref-tea")

    assert "pref-sneakers.md" in _search_paths(
        client, "vintage sneakers collection drops")

    out = _save(client, "Please forget that I collect vintage sneakers.",
                "forget-sneakers")
    assert out["forget"]["detected"] is True
    assert out["forget"]["pref"] == "I collect vintage sneakers"
    assert out["forget"]["archived"] == ["insights/pref-sneakers.md"]

    hits = _search_paths(client, "vintage sneakers collection drops")
    # The pref left default recall; the request is the retrievable tombstone.
    assert "pref-sneakers.md" not in hits
    assert "forget-sneakers.md" in hits

    pref_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "pref-sneakers.md"))
    assert pref_post["status"] == "archived"
    assert pref_post["superseded_by"] == "insights/forget-sneakers.md"
    # The tea memory shares no content words with the pref phrase: the
    # shared-word guard must have kept it out of resolution even though
    # max_targets had room for it.
    tea_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "pref-tea.md"))
    assert tea_post.get("status") != "archived"


def test_forget_hook_disabled_by_default(api_client):
    client, memory_dir = api_client
    assert config.consolidation.forget.enabled is False

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    out = _save(client, "Please forget that I collect vintage sneakers.",
                "forget-sneakers")
    assert "forget" not in out

    pref_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "pref-sneakers.md"))
    assert pref_post.get("status") != "archived"


def test_forget_hook_reports_empty_resolution(api_client, monkeypatch):
    """A detected request that resolves to nothing still reports itself —
    the request memory alone is the tombstone, and the save must not fail."""
    client, _ = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    # A shared-word floor no candidate clears: resolution comes back empty.
    monkeypatch.setattr(config.consolidation.forget, "min_shared_words", 999)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")
    out = _save(client, "Please forget that I collect vintage sneakers.",
                "forget-sneakers")
    assert out["forget"]["detected"] is True
    assert out["forget"]["archived"] == []


# ── granularity router ───────────────────────────────────────────────────────
#
# The dogfood failure shape (people and events below are fictional):
# forgetting is fact/entity-shaped, archival is file-shaped. A person-directed
# forget request resolves to dense ProjectSnapshots where the person is two
# mentions among dozens of content words; archiving them whole removed a
# closeout record and an incident log from recall. Coverage now routes:
# high-coverage targets archive whole, low-coverage targets get exactly their
# mentioning sentences struck in place and the rest of the file stays live.

_DENSE_SNAPSHOT = (
    "Moonrise Fair 2031 judging pipeline closeout. Handed the judging "
    "workflow to Wilhelmina Cragg after the June stage flood. Versions "
    "frozen at release candidate seven; ingest queue drained and verified "
    "against the submission manifest. Outstanding recovery items: rebuild "
    "thumbnail cache, reconcile duplicate performer registrations, restore "
    "the staging gallery snapshots, audit volunteer access grants. Flood "
    "log migrated to the tracker with twelve linked entries. Next steps: "
    "schedule retrospective, publish judging rubric, confirm pavilion "
    "contract renewal, decommission obsolete submission portals, rotate "
    "the shared credentials, document the escalation path Wilhelmina "
    "proposed during cleanup."
)


def test_granularity_router_retracts_mentions_in_dense_target(
        api_client, monkeypatch):
    """A dense shared memory that merely mentions the pref keeps its file but
    loses exactly the mentioning sentences — struck through with an opaque
    retraction marker — and unrelated recall keeps working."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")

    assert "moonrise-closeout.md" in _search_paths(
        client, "Moonrise Fair 2031 judging pipeline handoff")

    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina")
    assert out["forget"]["detected"] is True
    assert out["forget"]["archived"] == []
    assert out["forget"]["retracted"] == [
        {"path": "insights/moonrise-closeout.md", "mentions": 2}]

    dense_abs = os.path.join(memory_dir, "insights", "moonrise-closeout.md")
    post = frontmatter.load(dense_abs)
    # The file stays active; exactly the two mentioning sentences are struck.
    assert post.get("status") != "archived"
    assert post.content.count("[RETRACTED") == 2
    assert ("~~Handed the judging workflow to Wilhelmina Cragg after the "
            "June stage flood.~~") in post.content
    # The marker is opaque (no pref words) and terminator-final.
    import re as _re
    markers = _re.findall(r"\[RETRACTED [^\]]*\]\.", post.content)
    assert len(markers) == 2
    assert all("wilhelmina" not in m.lower() for m in markers)
    # The retraction is recorded as tracked state, not just markup.
    assert post["retracted_prefs"] == ["i know wilhelmina cragg"]
    # Untouched content is byte-identical, not rewritten around the strikes.
    assert ("Versions frozen at release candidate seven; ingest queue drained "
            "and verified against the submission manifest.") in post.content
    # Unrelated recall still reaches the memory.
    assert "moonrise-closeout.md" in _search_paths(
        client, "Moonrise Fair 2031 judging pipeline handoff")
    # The mutation carries its own audit sibling naming the pref.
    history = os.path.join(
        memory_dir, "insights", "moonrise-closeout-history.md")
    with open(history) as f:
        assert "I know Wilhelmina Cragg" in f.read()


def test_granularity_router_archives_focused_target_alongside_retraction(
        api_client, monkeypatch):
    """Surgical at both granularities in one request: the short pref-carrying
    memory archives whole while the dense co-resolved memory only loses its
    mentioning sentences."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "Met Wilhelmina Cragg at the fair; she judges the "
                  "Moonrise panel.", "pref-wilhelmina")
    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")

    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina")
    assert out["forget"]["archived"] == ["insights/pref-wilhelmina.md"]
    assert [r["path"] for r in out["forget"]["retracted"]] == [
        "insights/moonrise-closeout.md"]

    pref_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "pref-wilhelmina.md"))
    assert pref_post["status"] == "archived"
    dense_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "moonrise-closeout.md"))
    assert dense_post.get("status") != "archived"
    assert "[RETRACTED" in dense_post.content


def test_repeat_request_never_archives_a_retracted_file(
        api_client, monkeypatch):
    """The archive-flip regression: a re-saved request must not whole-file
    archive the memory the first pass protected. The retracted_prefs record
    keeps the file out of resolution entirely — it appears in no outcome
    list, its body keeps exactly the original markers, and it stays active."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    _save(client, "Please forget that I know Wilhelmina Cragg.",
          "forget-wilhelmina")
    out2 = _save(client, "Please forget that I know Wilhelmina Cragg.",
                 "forget-wilhelmina-again")

    dense_rel = "insights/moonrise-closeout.md"
    assert dense_rel not in out2["forget"]["archived"]
    assert dense_rel not in [
        r["path"] for r in out2["forget"].get("retracted", [])]
    assert dense_rel not in [
        s["path"] for s in out2["forget"].get("skipped", [])]

    post = frontmatter.load(
        os.path.join(memory_dir, "insights", "moonrise-closeout.md"))
    assert post.get("status") != "archived"
    assert post.content.count("[RETRACTED") == 2


def test_retraction_frees_target_slots_for_older_memories(
        api_client, monkeypatch):
    """The slot-starvation regression: after a dense file is retracted, an
    establishing memory for the same pref that the first pass could not reach
    (``max_targets`` exhausted) must be reachable on a later request — the
    retracted file no longer consumes a slot. The unreached memory predates
    the request: a request only ever resolves what existed when it was made
    (``tests/test_restore_surface_903.py`` covers the after-the-fact case)."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    monkeypatch.setattr(config.consolidation.forget, "max_targets", 1)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    _save(client, "Met Wilhelmina Cragg at the fair; she judges the "
                  "Moonrise panel.", "pref-wilhelmina")
    first = _save(client, "Please forget that I know Wilhelmina Cragg.",
                  "forget-wilhelmina")
    handled_first = set(first["forget"]["archived"]) | {
        r["path"] for r in first["forget"].get("retracted", [])}
    assert len(handled_first) == 1

    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina-again")
    handled_second = set(out["forget"]["archived"]) | {
        r["path"] for r in out["forget"].get("retracted", [])}
    assert len(handled_second) == 1
    assert handled_first | handled_second == {
        "insights/moonrise-closeout.md", "insights/pref-wilhelmina.md"}


def test_granularity_router_floor_zero_restores_whole_file_archival(
        api_client, monkeypatch):
    """min_target_coverage=0.0 disables the routing: every resolved target
    archives whole — proving the router, not resolution, made the decision."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)
    monkeypatch.setattr(config.consolidation.forget, "min_target_coverage", 0.0)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina")
    assert out["forget"]["archived"] == ["insights/moonrise-closeout.md"]
    assert "retracted" not in out["forget"]
    assert "skipped" not in out["forget"]

    post = frontmatter.load(
        os.path.join(memory_dir, "insights", "moonrise-closeout.md"))
    assert post["status"] == "archived"
    assert "[RETRACTED" not in post.content


def test_router_fails_closed_when_target_unreadable(api_client, monkeypatch):
    """A read error on the router's input produces the conservative outcome
    (skip + report), never the maximal-blast-radius one (whole-file archive
    on a target whose coverage was never measured)."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")

    with patch("palinode.consolidation.forget._file_body",
               side_effect=OSError("transient read failure")):
        out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                    "forget-wilhelmina")

    assert out["forget"]["archived"] == []
    assert out["forget"]["skipped"] == [
        {"path": "insights/moonrise-closeout.md", "status": "unreadable"}]
    post = frontmatter.load(
        os.path.join(memory_dir, "insights", "moonrise-closeout.md"))
    assert post.get("status") != "archived"
    assert "[RETRACTED" not in post.content


def test_retraction_refuses_living_replace_documents(api_client, monkeypatch):
    """A living (update_policy: replace) document is never struck — its next
    replace-save would silently regenerate the body and erase every marker.
    The pref is reported loudly unforgotten instead of archived or mangled."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, _DENSE_SNAPSHOT, "moonrise-closeout")
    dense_abs = os.path.join(memory_dir, "insights", "moonrise-closeout.md")
    post = frontmatter.load(dense_abs)
    post["update_policy"] = "replace"
    with open(dense_abs, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post) + "\n")

    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina")
    assert out["forget"]["archived"] == []
    skipped = out["forget"]["skipped"]
    assert [s["path"] for s in skipped] == ["insights/moonrise-closeout.md"]
    assert skipped[0]["status"] == "unforgotten"
    assert skipped[0]["reason"] == "replace_doc"

    post = frontmatter.load(dense_abs)
    assert post.get("status") != "archived"
    assert "[RETRACTED" not in post.content


def test_unforgotten_when_mention_lives_outside_the_strikeable_surface(
        api_client, monkeypatch):
    """Resolution matches on chunk content that includes heading text, but
    headings are not strikeable: a target whose only mention is a heading
    must be reported loudly unforgotten — never silently dropped, never
    whole-file archived."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    heading_only = (
        "## Wilhelmina Cragg handoff notes\n\n"
        "The judging pipeline closeout finished on schedule. Versions frozen "
        "at release candidate seven; ingest queue drained and verified "
        "against the submission manifest. Outstanding recovery items: "
        "rebuild thumbnail cache, reconcile duplicate performer "
        "registrations, restore the staging gallery snapshots, audit "
        "volunteer access grants. Flood log migrated to the tracker with "
        "twelve linked entries. Next steps: schedule retrospective, publish "
        "judging rubric, confirm pavilion contract renewal, decommission "
        "obsolete submission portals, rotate the shared credentials."
    )
    _save(client, heading_only, "handoff-notes")

    out = _save(client, "Please forget that I know Wilhelmina Cragg.",
                "forget-wilhelmina")
    assert out["forget"]["archived"] == []
    skipped = out["forget"].get("skipped", [])
    assert [s["path"] for s in skipped] == ["insights/handoff-notes.md"]
    assert skipped[0]["status"] == "unforgotten"
    assert skipped[0]["reason"] == "no_mentions"

    post = frontmatter.load(
        os.path.join(memory_dir, "insights", "handoff-notes.md"))
    assert post.get("status") != "archived"
    assert "[RETRACTED" not in post.content


# ── retraction walker, pure-function level ───────────────────────────────────

from palinode.consolidation.forget import _STOPWORDS, _content_words
from palinode.consolidation.retract import (
    _entity_tokens,
    _retract_in_body,
)

_MARKER = "[RETRACTED 2031-06-01 r:0a1b2c3d]."


def _walk(body: str, pref: str) -> tuple[str, int]:
    words = _content_words(pref)
    return _retract_in_body(
        body, words, _entity_tokens(pref, _STOPWORDS),
        min(2, len(words)) or 1, _MARKER,
    )


def test_entity_tokens_titlecase_only():
    # Mid-phrase Titlecase qualifies; ALL-CAPS emphasis and the phrase's
    # first word do not.
    assert _entity_tokens("I know Wilhelmina Cragg", _STOPWORDS) == {
        "wilhelmina", "cragg"}
    assert _entity_tokens("I HATE cilantro garnishes", _STOPWORDS) == set()
    assert _entity_tokens("Berlin was my home", _STOPWORDS) == set()


def test_walker_all_caps_pref_does_not_strike_on_single_common_word():
    body = "People hate the new deploy cadence. I dislike cilantro garnishes.\n"
    out, n = _walk(body, "I HATE cilantro garnishes")
    assert n == 1
    assert "~~People hate the new deploy cadence.~~" not in out
    assert "~~I dislike cilantro garnishes.~~" in out


def test_walker_structural_surfaces_are_never_struck():
    body = (
        "## Wilhelmina Cragg section\n"
        "| Wilhelmina Cragg | judge | active |\n"
        "```yaml\njudge: Wilhelmina Cragg\n```\n"
        "~~~yaml\njudge: Wilhelmina Cragg\n~~~\n"
        "<!-- palinode-auto-footer -->\n"
        "- [[people/wilhelmina-cragg]]\n"
    )
    out, n = _walk(body, "I know Wilhelmina Cragg")
    assert n == 0
    assert out == body


def test_walker_strikes_hard_wrapped_sentences_across_lines():
    body = "Started collecting vintage\nsneakers in college.\n"
    out, n = _walk(body, "I collect vintage sneakers")
    assert n == 1
    assert "~~Started collecting vintage\nsneakers in college.~~" in out


def test_walker_user_strikethrough_is_not_immune():
    body = ("Wilhelmina took over after we replaced the ~~draft rubric~~ "
            "final rubric.\n")
    out, n = _walk(body, "I know Wilhelmina Cragg")
    assert n == 1
    assert "[RETRACTED" in out


def test_walker_marked_spans_stay_put_and_neighbors_stay_matchable():
    """The marker never re-wraps, and — because it is terminator-final — the
    sentence after it still splits off and matches for a later pref."""
    body = "Wilhelmina judged the fair. Casper Nightingale ran the gate.\n"
    pass1, n1 = _walk(body, "I know Wilhelmina Cragg")
    assert n1 == 1
    pass2, n2 = _walk(pass1, "I know Casper Nightingale")
    assert n2 == 1
    assert "~~Casper Nightingale ran the gate.~~" in pass2
    # Re-running pass 1's pref on the result wraps nothing new.
    _, n3 = _walk(pass2, "I know Wilhelmina Cragg")
    assert n3 == 0


# ── embedder-boundary regression ────────────────────────────────────────────
#
# `resolve_forget_targets` is the exact call site the underlying papercut was
# found on, during a forget-arm measurement: `embedder.embed(pref)` feeding
# straight into `store.search_hybrid(pref, emb, ...)` with no check in
# between. A backend failure used to hand `search_hybrid` a zero-length
# vector, which sqlite-vec rejected with an OperationalError pointing at the
# SQL layer, two modules from the network failure that actually caused it.


def test_resolve_forget_targets_raises_before_reaching_search_when_embed_fails():
    """A backend embed failure must raise at the embedder boundary and never
    reach store.search_hybrid — the zero-length-vector crash this replaces."""

    def _boom(text, backend="local"):
        raise EmbeddingUnavailable(
            backend="local", model="bge-m3", text_len=len(text),
            cause="connection refused",
        )

    with patch("palinode.core.embedder.embed", side_effect=_boom), \
            patch("palinode.core.store.search_hybrid") as search_spy:
        with pytest.raises(EmbeddingUnavailable):
            forget.resolve_forget_targets("I collect vintage sneakers")

    search_spy.assert_not_called()


def test_forget_hook_survives_embedder_outage_during_resolution(api_client, monkeypatch):
    """The save-never-fails contract holds when the *forget resolution* embed
    call — not the save's own inline-index embed — fails. Same non-fatal
    degrade as an LLM/API error in the existing hook (see
    `palinode.api.routers.memory`'s `except Exception` around
    `check_forget_on_save`): the save succeeds, `forget` is simply absent from
    the response, and nothing crashes."""
    client, memory_dir = api_client
    monkeypatch.setattr(config.consolidation.forget, "enabled", True)

    _save(client, "I collect vintage sneakers and track new sneaker drops "
                  "every week.", "pref-sneakers")

    # Only the extracted pref phrase fails to embed — the forget-sneakers
    # file's own inline-index embed (a different string) still succeeds via
    # the fixture's normal hash-embed, isolating the failure to resolution.
    def _flaky(text, backend="local"):
        if text == "I collect vintage sneakers":
            raise EmbeddingUnavailable(
                backend="local", model="bge-m3", text_len=len(text),
                cause="connection refused",
            )
        return _hash_embed(text, backend)

    with patch("palinode.core.embedder.embed", side_effect=_flaky):
        out = _save(client, "Please forget that I collect vintage sneakers.",
                    "forget-sneakers")

    assert "forget" not in out, (
        "the forget hook's failure must not surface as a save failure"
    )
    # The request memory itself still landed — save-never-fails held.
    assert os.path.exists(
        os.path.join(memory_dir, "insights", "forget-sneakers.md")
    )
    # The pref memory was never resolved, so it must not have been archived.
    pref_post = frontmatter.load(
        os.path.join(memory_dir, "insights", "pref-sneakers.md"))
    assert pref_post.get("status") != "archived"

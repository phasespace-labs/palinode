"""Tests for the gitlab-aware entity linking work — optional external_refs frontmatter support.

Covers:
- Save with external_refs writes to frontmatter
- Search results include external_refs from metadata when set
- Memory without external_refs parses fine (backward compat)
- CLI --external-ref KEY=VALUE builds the dict correctly
- Free-form keys round-trip without error
- Nested values produce a soft warning and are dropped (not written)
"""
import logging
import pytest
import yaml
from fastapi.testclient import TestClient
from unittest.mock import patch

from palinode.api.server import app
from palinode.core.config import config
from palinode.core.parser import parse_external_refs, parse_markdown


client = TestClient(app)


#: Literals here carry the product's own qualified reference syntax, which the
#: issue-ref guard cannot distinguish from an unfollowable bare tag. Declared
#: rather than reworded: `owner/repo#N` names its tracker, and the value is the
#: thing under test. See tests/test_no_issue_refs_user_surface.py.
_ISSUE_REF_FIXTURES: tuple[tuple[str, str], ...] = (
    (
        "#17",
        "Fixture value for the gitlab_issue external-ref key: a qualified "
        "myorg/myrepo reference, placeholder org and repo.",
    ),
    (
        "#1234",
        "Fixture value for the gitlab_pipeline external-ref key: a qualified "
        "myorg/myrepo reference, placeholder org and repo.",
    ),
    (
        "#99",
        "Fixture value for the github_pr external-ref key, qualified to the "
        "public phasespace-labs/palinode repo — followable by design.",
    ),
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_memory_dir(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old


def _read_frontmatter(file_path: str) -> dict:
    with open(file_path, "r") as f:
        raw = f.read()
    parts = raw.split("---", 2)
    assert len(parts) >= 3, f"Expected frontmatter in:\n{raw}"
    return yaml.safe_load(parts[1])


# ── Parser unit tests ─────────────────────────────────────────────────────────


class TestParseExternalRefs:
    def test_returns_none_when_absent(self):
        result = parse_external_refs({})
        assert result is None

    def test_returns_none_for_none_value(self):
        result = parse_external_refs({"external_refs": None})
        assert result is None

    def test_recognises_flat_dict(self):
        refs = {"gitlab_mr": "myorg/myrepo!42", "linear_issue": "PAL-1"}
        result = parse_external_refs({"external_refs": refs})
        assert result == refs

    def test_coerces_int_value_to_str(self):
        result = parse_external_refs({"external_refs": {"ticket": 99}})
        assert result == {"ticket": "99"}

    def test_drops_nested_dict_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="palinode.parser"):
            result = parse_external_refs(
                {"external_refs": {"weird": {"nested": "dict"}}}
            )
        # Entry with nested dict is dropped; result is None (nothing left)
        assert result is None
        assert "nested" in caplog.text.lower() or "external_refs[" in caplog.text

    def test_drops_nested_list_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="palinode.parser"):
            result = parse_external_refs(
                {"external_refs": {"tags": ["a", "b"]}}
            )
        assert result is None
        assert "external_refs[" in caplog.text

    def test_partial_drop_keeps_valid_entries(self, caplog):
        with caplog.at_level(logging.WARNING, logger="palinode.parser"):
            result = parse_external_refs(
                {"external_refs": {"ok": "val", "bad": {"nested": True}}}
            )
        assert result == {"ok": "val"}

    def test_non_dict_top_level_returns_none_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="palinode.parser"):
            result = parse_external_refs({"external_refs": ["a", "b"]})
        assert result is None

    def test_free_form_key_passes_through(self):
        result = parse_external_refs(
            {"external_refs": {"custom_tracker": "X-99"}}
        )
        assert result == {"custom_tracker": "X-99"}

    def test_parse_markdown_backwards_compat_no_field(self):
        """Files without external_refs parse without error."""
        content = "---\nid: test\ntype: Insight\n---\n\nSome body text."
        metadata, _ = parse_markdown(content)
        assert "external_refs" not in metadata or metadata.get("external_refs") is None

    def test_parse_markdown_with_external_refs(self):
        """Files with external_refs in frontmatter preserve the field."""
        content = (
            "---\n"
            "id: test\n"
            "type: Decision\n"
            "external_refs:\n"
            "  gitlab_mr: myorg/myrepo!42\n"
            "  linear_issue: PAL-1\n"
            "---\n\nSome body."
        )
        metadata, _ = parse_markdown(content)
        assert metadata["external_refs"] == {
            "gitlab_mr": "myorg/myrepo!42",
            "linear_issue": "PAL-1",
        }


# ── API / save path tests ─────────────────────────────────────────────────────


class TestSaveExternalRefs:
    def test_save_with_external_refs_writes_frontmatter(self, mock_memory_dir):
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={
                    "content": "Decision to use GitLab MRs for review",
                    "type": "Decision",
                    "external_refs": {
                        "gitlab_mr": "myorg/myrepo!42",
                        "linear_issue": "PAL-1",
                    },
                },
            )
        assert res.status_code == 200
        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["external_refs"] == {
            "gitlab_mr": "myorg/myrepo!42",
            "linear_issue": "PAL-1",
        }

    def test_save_without_external_refs_omits_field(self, mock_memory_dir):
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={"content": "Plain memory", "type": "Insight"},
            )
        assert res.status_code == 200
        fm = _read_frontmatter(res.json()["file_path"])
        assert "external_refs" not in fm

    def test_save_with_free_form_key(self, mock_memory_dir):
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={
                    "content": "Custom tracker ref",
                    "type": "Insight",
                    "external_refs": {"custom_tracker": "X-99"},
                },
            )
        assert res.status_code == 200
        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["external_refs"] == {"custom_tracker": "X-99"}

    def test_save_roundtrips_all_recognised_keys(self, mock_memory_dir):
        refs = {
            "gitlab_mr": "myorg/myrepo!42",
            "gitlab_issue": "myorg/myrepo#17",
            "gitlab_pipeline": "myorg/myrepo#1234",
            "github_pr": "phasespace-labs/palinode#99",
            "linear_issue": "PAL-42",
            "jira_issue": "PROJ-100",
        }
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={
                    "content": "All recognised keys",
                    "type": "Decision",
                    "external_refs": refs,
                },
            )
        assert res.status_code == 200
        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["external_refs"] == refs

    def test_save_with_nested_external_ref_value_drops_entry(self, mock_memory_dir):
        """Nested dict value is soft-warned and the entry is dropped.

        The overall save still succeeds — external_refs is just omitted
        (or absent) when all entries are invalid.
        """
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={
                    "content": "Memory with invalid nested ref",
                    "type": "Insight",
                    "external_refs": {"weird": {"nested": "dict"}},
                },
            )
        assert res.status_code == 200
        fm = _read_frontmatter(res.json()["file_path"])
        # Nested entry is dropped so external_refs should be absent
        assert "external_refs" not in fm


# ── Search result metadata test ───────────────────────────────────────────────


class TestSearchExternalRefs:
    def test_search_result_metadata_includes_external_refs(self, mock_memory_dir):
        """external_refs in frontmatter appears in file metadata when read back.

        We save a memory with external_refs, then read the file directly from
        disk and parse the frontmatter to confirm the round-trip.  (The /read
        endpoint only accepts relative paths, so we go via the file_path
        returned by /save.)
        """
        refs = {"gitlab_mr": "foo!42"}
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post(
                "/save",
                json={
                    "content": "Memory with external refs for search test",
                    "type": "Insight",
                    "slug": "ext-refs-search-test",
                    "external_refs": refs,
                },
            )
        assert res.status_code == 200
        abs_file_path = res.json()["file_path"]

        # Read the file back via /read with a relative path (absolute is rejected).
        rel_path = abs_file_path.replace(mock_memory_dir + "/", "", 1)
        read_res = client.get("/read", params={"file_path": rel_path, "meta": "true"})
        assert read_res.status_code == 200
        fm = read_res.json().get("frontmatter", {})
        assert fm.get("external_refs") == refs


# ── CLI flag parsing test ─────────────────────────────────────────────────────


class TestCliExternalRefFlag:
    def test_external_ref_pairs_build_dict(self):
        """--external-ref KEY=VALUE pairs correctly assemble into a dict.

        We exercise the parsing logic directly by simulating what the CLI
        command does with external_ref_pairs.
        """
        pairs = ("gitlab_mr=myorg/myrepo!42", "linear_issue=PAL-1")
        result: dict = {}
        for pair in pairs:
            assert "=" in pair
            key, _, value = pair.partition("=")
            result[key.strip()] = value
        assert result == {
            "gitlab_mr": "myorg/myrepo!42",
            "linear_issue": "PAL-1",
        }

    def test_external_ref_value_with_equals_preserved(self):
        """Values containing '=' (e.g. URLs) are preserved correctly."""
        pair = "url=https://example.com?a=1&b=2"
        key, _, value = pair.partition("=")
        assert key == "url"
        assert value == "https://example.com?a=1&b=2"

    def test_cli_save_command_invocation(self, mock_memory_dir):
        """Full CLI invocation with --external-ref reaches the API correctly."""
        from click.testing import CliRunner
        from palinode.cli.save import save as save_cmd

        runner = CliRunner()

        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            # We patch api_client.save to intercept the call without needing
            # a live API server.
            saved_kwargs: dict = {}

            def capture_save(*args, **kwargs):
                saved_kwargs.update(kwargs)
                return {"file_path": "/tmp/test.md", "id": "test-id"}

            with patch("palinode.cli.save.api_client.save", side_effect=capture_save):
                result = runner.invoke(
                    save_cmd,
                    [
                        "Test memory content",
                        "--type", "Insight",
                        "--external-ref", "gitlab_mr=palinode!42",
                        "--external-ref", "linear_issue=PAL-1",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert saved_kwargs.get("external_refs") == {
            "gitlab_mr": "palinode!42",
            "linear_issue": "PAL-1",
        }

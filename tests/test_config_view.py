"""Regression test for #274: `palinode config view` must not crash.

Root cause: `from palinode.cli.list import list_cmd` had the side effect of
binding `palinode.cli.list` (the submodule) onto the `palinode.cli` package
namespace, shadowing the builtin `list` for any name-lookup inside the
package. The nested `to_dict` helper in `config_view` then resolved `list`
to the submodule, and `isinstance(obj, <module>)` raised TypeError. Fixed
by renaming the submodule to `list_cmd.py` (+ defensive `builtins.list` +
try/except fallback inside `to_dict`).
"""
from __future__ import annotations

from click.testing import CliRunner


def test_config_view_yaml_renders_completely():
    """Default `palinode config view` must succeed and produce yaml output."""
    from palinode.cli import config_view

    runner = CliRunner()
    result = runner.invoke(config_view, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Expect at least one section header from the rendered config.
    assert "memory_dir:" in result.output or "search:" in result.output


def test_config_view_json_renders_completely():
    """`--format=json` path must succeed too."""
    from palinode.cli import config_view

    runner = CliRunner()
    result = runner.invoke(config_view, ["--format", "json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "{" in result.output


def test_palinode_cli_list_attribute_does_not_shadow_builtin():
    """Guard the latent bug: the rename means `palinode.cli.list` should NOT
    exist as a submodule anymore. If a future change re-introduces a
    `palinode/cli/list.py`, this test fails loud so we don't re-ship #274.
    """
    import palinode.cli

    # After the rename, the attribute either doesn't exist or is something
    # other than a module. Either way, the canonical `list_cmd` lives at
    # palinode.cli.list_cmd.
    assert hasattr(palinode.cli, "list_cmd")
    # Defensive: if `list` does still resolve on palinode.cli for some
    # reason (e.g. a click command bound to that name), it must NOT be a
    # module — that's the specific shape that caused #274.
    import types
    list_attr = getattr(palinode.cli, "list", None)
    assert not isinstance(list_attr, types.ModuleType), (
        "palinode.cli.list resolves to a submodule again — this is the "
        "exact shape that broke palinode config view (#274). Rename it."
    )

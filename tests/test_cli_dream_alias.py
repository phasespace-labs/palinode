from click.testing import CliRunner

from palinode.cli import main


def test_dream_is_the_consolidate_command() -> None:
    assert main.commands["dream"] is main.commands["consolidate"]


def test_dream_help_points_to_the_canonical_command() -> None:
    result = CliRunner().invoke(main, ["dream", "--help"])

    assert result.exit_code == 0
    assert "alias for this command" in result.output

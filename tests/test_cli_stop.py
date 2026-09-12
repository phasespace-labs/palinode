import subprocess

from click.testing import CliRunner

from palinode.cli import main


def test_stop_without_systemctl_exits_nonzero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "systemctl" in result.output


def test_stop_success_exits_zero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: None)

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_stop_systemctl_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr("subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "Failed to stop" in result.output


def test_stop_continues_with_remaining_services_after_failure(monkeypatch):
    """A failed service must not prevent the remaining services from being stopped."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/systemctl")

    def fake_run(args, check=True):
        # palinode-api.service is stopped first; make only it fail.
        if "api" in args[-1]:
            raise subprocess.CalledProcessError(1, args)
        return None

    monkeypatch.setattr("subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "Failed to stop palinode-api.service" in result.output
    assert "✓ palinode-watcher.service stopped" in result.output

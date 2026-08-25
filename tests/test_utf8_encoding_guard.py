"""Guard: text-mode file and subprocess I/O in ``palinode/`` names its encoding.

Palinode writes memory files as UTF-8 through ``write_memory_file`` and its
own save path emits em dashes and emoji freely. A read site that calls
``open()`` with no ``encoding=`` decodes with the *locale* default — cp1252 on
native Windows — so the first non-ASCII memory raises ``UnicodeDecodeError``
mid-index, mid-consolidate, or mid-search. See
https://github.com/phasespace-labs/palinode/issues/93.

Two layers:

1. An AST scan of every ``palinode/*.py`` for text-mode I/O calls that omit
   ``encoding=``: builtin ``open``, ``Path.open``, ``os.fdopen``,
   ``Path.read_text`` / ``write_text``, text-mode ``tempfile`` factories, and
   ``subprocess`` calls that ask for decoded output (``text=True`` /
   ``universal_newlines=True``). Binary-mode calls are exempt (nothing to
   decode). Genuinely non-file ``.open`` calls (``os.open`` returns an fd;
   ``fitz.open`` opens a PDF) go through ``_ALLOWLIST``.
2. A behavioural check that runs a real write-then-read round trip of a
   non-ASCII memory under an ASCII locale in a subprocess. It fails the way a
   cp1252 host fails, so a regression in a swept site is caught on Linux and
   macOS CI without a Windows runner.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "palinode"

# (relative path, substring of the call source) pairs that are exempt. Each
# entry must match at least one flagged call while the file exists; an entry
# whose file has been deleted is ignored so a pending removal does not have to
# be sequenced against this guard.
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    # PyMuPDF document handle, not a text file.
    ("palinode/ingest/pipeline.py", "fitz.open("),
    # mem0 one-off importer, scheduled for deletion; not swept on purpose.
    ("palinode/migration/mem0_classify.py", "open("),
    ("palinode/migration/mem0_export.py", "open("),
    ("palinode/migration/mem0_generate.py", "open("),
)

# Test files whose reads and writes have been swept, listed one area at a time
# as the native-Windows cleanup works through them. A file joins this tuple in
# the same PR that sweeps it, so a later edit cannot quietly reintroduce a
# locale-default call into an area already fixed. The rest of ``tests/`` is
# deliberately absent: the scope is helpers exchanging files Palinode itself
# wrote as UTF-8, not every locale-default call in the test tree.
_SWEPT_TEST_FILES: tuple[str, ...] = (
    "tests/test_executor.py",
    "tests/test_executor_replace_guard.py",
)

_TEXT_MODE_TEMPFILE = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
_SUBPROCESS_TEXT = {"run", "Popen", "check_output", "call", "check_call"}


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _kw(node: ast.Call, name: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const(node: ast.expr | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _mode(node: ast.Call, positional_index: int) -> str | None:
    """The literal ``mode`` argument, or None if absent / non-literal."""
    explicit = _const(_kw(node, "mode"))
    if isinstance(explicit, str):
        return explicit
    if len(node.args) > positional_index:
        value = _const(node.args[positional_index])
        if isinstance(value, str):
            return value
    return None


def _is_unencoded_text_io(node: ast.Call) -> bool:
    """True when *node* is a text-mode I/O call with no ``encoding=``."""
    if _kw(node, "encoding") is not None:
        return False
    func = node.func
    name = _dotted(func)

    if name == "open":
        mode = _mode(node, 1)
    elif name == "os.fdopen":
        mode = _mode(node, 1)
    elif isinstance(func, ast.Attribute) and func.attr == "open" and name != "os.open":
        mode = _mode(node, 0)
    elif isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        return True
    elif name in _TEXT_MODE_TEMPFILE or name in {f"tempfile.{n}" for n in _TEXT_MODE_TEMPFILE}:
        mode = _mode(node, 0)
        # tempfile factories default to binary ("w+b"); only an explicit
        # text mode decodes anything.
        return mode is not None and "b" not in mode
    elif name in {f"subprocess.{n}" for n in _SUBPROCESS_TEXT}:
        return _const(_kw(node, "text")) is True or _const(_kw(node, "universal_newlines")) is True
    else:
        return False
    return mode is None or "b" not in mode


def _scan(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_unencoded_text_io(node):
            segment = ast.get_source_segment(source, node) or _dotted(node.func)
            found.append((node.lineno, segment.splitlines()[0]))
    return found


def _allowlisted(rel: str, segment: str, used: set[tuple[str, str]]) -> bool:
    for entry in _ALLOWLIST:
        if entry[0] == rel and entry[1] in segment:
            used.add(entry)
            return True
    return False


def test_source_text_io_names_its_encoding() -> None:
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, segment in _scan(path):
            if not _allowlisted(rel, segment, used):
                offenders.append(f"{rel}:{lineno}: {segment}")
    assert not offenders, (
        "text-mode I/O without encoding= (locale default; cp1252 on native "
        "Windows) — add encoding=\"utf-8\" (git/subprocess output: also "
        "errors=\"replace\"), or allowlist a genuinely non-text site:\n  "
        + "\n  ".join(offenders)
    )

    stale = [
        entry for entry in _ALLOWLIST
        if entry not in used and (REPO_ROOT / entry[0]).exists()
    ]
    assert not stale, f"allowlist entries no longer match anything: {stale}"


def test_guard_flags_locale_default_reads(tmp_path: Path) -> None:
    """The scanner catches the shapes it exists to catch."""
    src = tmp_path / "sample.py"
    src.write_text(
        "import os, subprocess, tempfile\n"
        "from pathlib import Path\n"
        "open('a')\n"                                     # flagged
        "open('a', 'r')\n"                                # flagged
        "open('a', mode='w')\n"                           # flagged
        "open('a', 'rb')\n"                               # binary: fine
        "open('a', encoding='utf-8')\n"                   # fine
        "Path('a').read_text()\n"                         # flagged
        "Path('a').write_text('x')\n"                     # flagged
        "Path('a').open('a')\n"                           # flagged
        "os.open('a', os.O_RDONLY)\n"                     # fd: fine
        "os.fdopen(3, 'w')\n"                             # flagged
        "os.fdopen(3, 'wb')\n"                            # binary: fine
        "tempfile.NamedTemporaryFile(mode='w')\n"         # flagged
        "tempfile.NamedTemporaryFile()\n"                 # binary default: fine
        "subprocess.run(['x'], text=True)\n"              # flagged
        "subprocess.run(['x'], capture_output=True)\n"    # bytes: fine
        "subprocess.run(['x'], text=True, encoding='utf-8', errors='replace')\n",  # fine
        encoding="utf-8",
    )
    flagged = [line for line, _ in _scan(src)]
    assert flagged == [3, 4, 5, 8, 9, 10, 12, 14, 16]


_ROUNDTRIP = r"""
import locale, sys
from palinode.core.config import config
from palinode.core import git_tools
from palinode.consolidation.fact_ids import add_fact_ids_to_file

# The interpreter must actually be in a non-UTF-8 locale for this to prove
# anything; report and bail rather than pass vacuously.
enc = locale.getpreferredencoding(False).lower().replace("-", "").replace("_", "")
if sys.flags.utf8_mode or enc in ("utf8",):
    print("SKIP:" + enc)
    sys.exit(0)

config.memory_dir = sys.argv[1]
config.git.auto_commit = False
path = sys.argv[1] + "/insights/unicode.md"
import os
os.makedirs(os.path.dirname(path), exist_ok=True)
git_tools.write_memory_file(path, "---\ntitle: t\n---\n\n- em dash — brain \U0001f9e0 日本語\n")
count = add_fact_ids_to_file(path)
with open(path, encoding="utf-8") as f:
    body = f.read()
assert count == 1, count
assert "日本語 <!-- fact:" in body, body
print("OK")
"""


def test_non_ascii_memory_roundtrips_under_ascii_locale(tmp_path: Path) -> None:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("LC_", "LANG", "PYTHONUTF8", "PYTHONIOENCODING"))
    }
    env.update(
        LC_ALL="C", LANG="C",
        PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
        PYTHONPATH=str(REPO_ROOT),
    )
    # The script goes through a UTF-8 source file, not `-c`: under LC_ALL=C the
    # child cannot decode non-ASCII argv (surrogateescape), which would fail
    # before the code under test runs. Source files are UTF-8 by definition.
    script = tmp_path / "roundtrip.py"
    script.write_text(_ROUNDTRIP, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    out = proc.stdout.strip()
    if out.startswith("SKIP:"):
        pytest.skip(f"interpreter still UTF-8 under LC_ALL=C ({out[5:]}); cannot force a locale-default failure")
    assert proc.returncode == 0 and out.endswith("OK"), (
        f"non-ASCII memory failed to round-trip under an ASCII locale\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_swept_test_files_name_their_encoding() -> None:
    """Test areas already swept for native Windows stay swept."""
    offenders: list[str] = []
    missing: list[str] = []
    for rel in _SWEPT_TEST_FILES:
        path = REPO_ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue
        for lineno, segment in _scan(path):
            offenders.append(f"{rel}:{lineno}: {segment}")
    assert not missing, f"swept file no longer exists; update _SWEPT_TEST_FILES: {missing}"
    assert not offenders, (
        "text-mode I/O without encoding= in a test area already swept for "
        'native Windows - add encoding="utf-8":\n  ' + "\n  ".join(offenders)
    )

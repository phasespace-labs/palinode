"""
Check modules.

Importing this package triggers the registration side-effects for every
check module listed here.  Add new check modules by importing them below.
"""
from palinode.diagnostics.checks import memory_dir  # noqa: F401
from palinode.diagnostics.checks import db_path  # noqa: F401
from palinode.diagnostics.checks import phantom_db  # noqa: F401
from palinode.diagnostics.checks import multiple_dirs  # noqa: F401
from palinode.diagnostics.checks import service  # noqa: F401
from palinode.diagnostics.checks import watcher  # noqa: F401
from palinode.diagnostics.checks import config_drift  # noqa: F401
from palinode.diagnostics.checks import mcp_config_check  # noqa: F401
from palinode.diagnostics.checks import process_env  # noqa: F401
from palinode.diagnostics.checks import index_size  # noqa: F401
from palinode.diagnostics.checks import reindex_state  # noqa: F401
from palinode.diagnostics.checks import git_remote  # noqa: F401
from palinode.diagnostics.checks import claude_md  # noqa: F401
from palinode.diagnostics.checks import audit_log  # noqa: F401

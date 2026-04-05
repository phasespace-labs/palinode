import sys
import json
from enum import Enum
from typing import Any, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()

class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"

def get_default_format() -> OutputFormat:
    """Detect if output is a TTY and return default format."""
    if sys.stdout.isatty():
        return OutputFormat.TEXT
    return OutputFormat.JSON

def print_result(data: Any, fmt: Optional[OutputFormat] = None):
    """Print data in the requested or default format."""
    if fmt is None:
        fmt = get_default_format()
    
    if fmt == OutputFormat.JSON:
        console.print(json.dumps(data, indent=2))
    else:
        # Commands should implement their own custom printing for TEXT
        # This is a fallback
        if isinstance(data, (dict, list)):
            console.print(data)
        else:
            console.print(str(data))

def print_error(msg: str):
    console.print(f"[red]Error:[/red] {msg}")

def print_success(msg: str):
    console.print(f"[green]✓[/green] {msg}")

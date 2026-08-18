"""
Palinode Ingestion Pipeline

Processes files dropped into inbox/raw/ or passed directly to endpoints:
  PDF → extract text → summarize → write to research/
  Audio → Transcriptor API → transcript → write to research/
  URL (.url/.webloc/text) → fetch → readability → write to research/
  Markdown/text → classify → file into appropriate bucket

Each ingested document produces a research reference file with provenance.
"""
from __future__ import annotations

import os
import re
import time
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
import urllib.parse
import socket
import ipaddress

from palinode.core import git_tools
from palinode.core.config import config
from palinode.core.hashing import stable_md5_hexdigest

logger = logging.getLogger("palinode.ingest")

def is_safe_url(url: str) -> bool:
    """Validates URL for SSRF protection."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
            
        hostname = parsed.hostname
        if not hostname:
            return False
            
        try:
            ip = socket.gethostbyname(hostname)
        except socket.gaierror:
            return False
            
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast:
            return False
            
        return True
    except Exception:
        return False


def process_inbox() -> None:
    """Scan ingestion directories for new files."""
    raw_dir = os.path.join(config.palinode_dir, config.ingestion.inbox_dir)
    processed_dir = os.path.join(config.palinode_dir, config.ingestion.processed_dir)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    for filename in os.listdir(raw_dir):
        filepath = os.path.join(raw_dir, filename)
        if not os.path.isfile(filepath):
            continue

        logger.info(f"Processing: {filename}")
        try:
            result = process_file(filepath)
            if result:
                dest = os.path.join(processed_dir, filename)
                os.rename(filepath, dest)
                logger.info(f"Done: {filename} → {result}")
            else:
                logger.warning(f"No result for: {filename}")
        except Exception as e:
            logger.error(f"Failed to process {filename}: {e}")


def process_file(filepath: str) -> str | None:
    """Invoke parser based on file extension.

    Args:
        filepath (str): Path to the input file.

    Returns:
        str | None: Returns absolute path of the saved file if successful, or None.
    """
    ext = Path(filepath).suffix.lower()
    name = Path(filepath).stem

    if ext in (".pdf",):
        return ingest_pdf(filepath, name)
    elif ext in (".m4a", ".mp3", ".wav", ".ogg", ".flac"):
        return ingest_audio(filepath, name)
    elif ext in (".mp4", ".mkv", ".mov", ".webm"):
        return ingest_audio(filepath, name) # Extract audio track
    elif ext in (".md", ".txt"):
        return ingest_text(filepath, name)
    elif ext in (".url", ".webloc"):
        return ingest_url_file(filepath, name)
    else:
        logger.warning(f"Unknown file type: {ext}")
        return None


def ingest_pdf(filepath: str, name: str) -> str | None:
    """Extract text blocks from unparsed PDF layouts formatted as Markdown.

    Args:
        filepath (str): Path to input PDF.
        name (str): Document basename.

    Returns:
        str | None: Destination saved file path if completed successfully.
    """
    try:
        try:
            import fitz  # pymupdf
            doc = fitz.open(filepath)
            text = "\n\n".join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            result = subprocess.run(
                ["pdftotext", filepath, "-"],
                capture_output=True, text=True, timeout=60
            )
            text = result.stdout

        if not text.strip():
            logger.warning(f"Empty PDF: {filepath}")
            return None

        # Cap for very large PDFs
        capped_len = config.ingestion.pdf_max_chars
        
        return write_research_file(
            name=name,
            content=text[:capped_len],
            source_file=os.path.basename(filepath),
            file_type="pdf",
        )
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return None


def ingest_audio(filepath: str, name: str) -> str | None:
    """Send audio file to the remote transcription service.

    Args:
        filepath (str): Path to the media file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    url = config.ingestion.transcriptor.url
    timeout_sec = config.ingestion.transcriptor.timeout_seconds
    
    try:
        with open(filepath, "rb") as f:
            response = httpx.post(
                f"{url}/transcribe",
                files={"file": (os.path.basename(filepath), f)},
                timeout=httpx.Timeout(float(timeout_sec), connect=10.0),
            )
            response.raise_for_status()
            data = response.json()

        text = data.get("text", "")
        if not text:
            logger.warning(f"Empty transcript: {filepath}")
            return None

        return write_research_file(
            name=name,
            content=text,
            source_file=os.path.basename(filepath),
            file_type="audio_transcript",
        )
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return None


def ingest_text(filepath: str, name: str) -> str | None:
    """Ingest a text file, delegating to URL ingestion if content is a URL.

    Args:
        filepath (str): Path to the input file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    with open(filepath, "r") as f:
        content = f.read()

    stripped = content.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return ingest_url(stripped, name)

    return write_research_file(
        name=name,
        content=content,
        source_file=os.path.basename(filepath),
        file_type="text",
    )


def ingest_url_file(filepath: str, name: str) -> str | None:
    """Read macOS .webloc or Windows .url shortcut files.

    Args:
        filepath (str): Path to the shortcut file.
        name (str): Document basename.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    with open(filepath, "r") as f:
        content = f.read()

    # .url format
    url_match = re.search(r"URL=(.+)", content)
    if url_match:
        return ingest_url(url_match.group(1).strip(), name)

    # .webloc is XML plist
    url_match = re.search(r"<string>(https?://[^<]+)</string>", content)
    if url_match:
        return ingest_url(url_match.group(1).strip(), name)

    logger.warning(f"Could not extract URL from: {filepath}")
    return None


def ingest_url(url: str, name: str) -> str | None:
    """Fetch web page content and clean main text using readability.

    Args:
        url (str): HTTP or HTTPS URL to fetch and convert into a research file.
        name (str): Fallback document slug.

    Returns:
        str | None: Path to the saved research file, or None.
    """
    if not is_safe_url(url):
        logger.error(f"URL fetch blocked by SSRF protection: {url}")
        return None

    try:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        html = response.text

        # Simple readability: strip HTML tags
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) < 100:
            logger.warning(f"Too little content from URL: {url}")
            return None

        capped_len = config.ingestion.url_max_chars
        return write_research_file(
            name=name,
            content=text[:capped_len],
            source_url=url,
            file_type="url",
        )
    except Exception as e:
        logger.error(f"URL fetch failed for {url}: {e}")
        return None


def write_research_file(
    name: str,
    content: str,
    source_file: str = "",
    source_url: str = "",
    file_type: str = "text",
) -> str:
    """Write research Markdown file with YAML frontmatter.

    Args:
        name (str): Title used to derive the research file name and heading.
        content (str): Text written to the body of the research file.
        source_file (str): Path to the source file.
        source_url (str): Optional URL from which the research content was fetched.
        file_type (str): Source file extension or type.

    Returns:
        str: Path to the created research file.
    """
    today = time.strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())[:50].strip("-")
    filename = f"{today}-{slug}.md"
    filepath = os.path.join(config.palinode_dir, "research", filename)

    if os.path.exists(filepath):
        slug += f"-{stable_md5_hexdigest(content[:100])[:6]}"
        filename = f"{today}-{slug}.md"
        filepath = os.path.join(config.palinode_dir, "research", filename)

    fm = {
        "id": f"research-{slug}",
        "category": "research",
        "source_url": source_url or "",
        "source_file": source_file or "",
        "source_type": file_type,
        "date": today,
        "tags": [],
        # timezone-aware UTC ISO-8601. Previously used
        # ``time.strftime("...Z")`` which emits local time stamped as UTC.
        "last_updated": datetime.now(UTC).isoformat(),
    }

    doc = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n# {name}\n\n{content}\n"

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    git_tools.write_memory_file(filepath, doc)

    return filepath


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    process_inbox()

#!/usr/bin/env python3
"""MCP web research server for local AI clients.

Tools:
  - search_web: query DuckDuckGo's HTML results.
  - fetch_url: fetch a page and extract readable text.
  - save_note: save a markdown/text note under MCP_NOTES_DIR.

Run with stdio transport:
  python server.py
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
try:
    # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - compatibility with older SDK releases
    # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP

NOTES_DIR = Path(os.environ.get("MCP_NOTES_DIR", str(Path.home() / "MCPWebResearch" / "notes"))).expanduser()
NOTES_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 MCPWebResearch/1.0"
)

mcp = FastMCP("web-research")


def _result_url(href: str) -> str:
    """Convert DuckDuckGo result redirect links to direct URLs when possible."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _extract_text(html: str, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "form"]):
        unwanted.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    if title:
        text = f"# {title}\n\n{text}"
    return text[:max_chars].strip()


@mcp.tool()
async def search_web(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the public web. Returns titles, URLs, and snippets.

    Args:
        query: Search query. Keep it concise and specific.
        max_results: Number of results to return, from 1 to 10.
    """
    query = query.strip()
    if not query:
        return {"error": "query must not be empty"}
    max_results = max(1, min(int(max_results or 5), 10))

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "us-en"},
        )
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for result in soup.select(".result"):
        link = result.select_one(".result__a")
        if not link:
            continue
        url = _result_url(link.get("href", ""))
        if not url or url in seen:
            continue
        seen.add(url)

        snippet_el = result.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        results.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": url,
                "snippet": snippet,
            }
        )
        if len(results) >= max_results:
            break

    return {"query": query, "results": results}


@mcp.tool()
async def fetch_url(url: str, max_chars: int = 8000) -> dict[str, Any]:
    """Fetch a public web page and return extracted text.

    Args:
        url: Full http(s) URL to fetch.
        max_chars: Maximum characters of page text to return, from 500 to 30000.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"error": "url must start with http:// or https://"}

    max_chars = max(500, min(int(max_chars or 8000), 30000))
    async with httpx.AsyncClient(
        timeout=25.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")

    if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
        return {
            "url": str(response.url),
            "content_type": content_type,
            "text": f"[Fetched {len(response.content)} bytes of non-text content: {content_type}]",
        }

    text = _extract_text(response.text, max_chars)
    return {
        "url": str(response.url),
        "status_code": response.status_code,
        "content_type": content_type,
        "chars_returned": len(text),
        "text": text,
    }


@mcp.tool()
async def save_note(filename: str, content: str) -> dict[str, Any]:
    """Save a markdown or text note in the configured notes directory.

    The filename is sanitized and path traversal is blocked. This is intended
    for research notes, summaries, and citations.

    Args:
        filename: Note filename such as 'llm-survey.md'.
        content: Markdown/text content to save.
    """
    raw_name = Path(filename or "").name.strip()
    if not raw_name:
        raw_name = f"note-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    if "." not in raw_name:
        raw_name += ".md"

    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", raw_name).strip(".-") or "note.md"
    target = (NOTES_DIR / safe_name).resolve()

    # The resolve() check prevents ../ traversal even after sanitization.
    if NOTES_DIR.resolve() not in target.parents and target.parent != NOTES_DIR.resolve():
        return {"error": "path traversal is not allowed", "notes_dir": str(NOTES_DIR)}

    target.write_text(content, encoding="utf-8")
    return {"saved": True, "path": str(target), "bytes": target.stat().st_size}


@mcp.resource("config://notes-directory")
def notes_directory() -> str:
    """Return the directory where research notes are saved."""
    return str(NOTES_DIR)


if __name__ == "__main__":
    # stdio is the standard transport for local MCP clients such as Claude Desktop.
    mcp.run(transport="stdio")

"""Web tools — web_search and web_fetch.

web_search: Search via external API (Brave Search by default).
web_fetch: Fetch URL and convert HTML to readable text.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger(__name__)

# Set at startup
_search_api_key: str = ""
_search_provider: str = "brave"


def configure(api_key: str = "", provider: str = "brave") -> None:
    global _search_api_key, _search_provider
    _search_api_key = api_key
    _search_provider = provider


# ─── SSRF Protection ────────────────────────────────────────────

_ALLOWED_SCHEMES = {"http", "https"}


def _is_private_ip(addr: str) -> bool:
    """Check if an IP address is private/loopback/reserved.

    Handles non-standard encodings (octal, hex, decimal) that
    ipaddress rejects but socket.inet_aton accepts.
    """
    try:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            # Normalize octal/hex/decimal encodings via inet_aton
            normalized = socket.inet_ntoa(socket.inet_aton(addr))
            ip = ipaddress.ip_address(normalized)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except Exception:
        return True  # Fail closed: unknown format = block


def _validate_url(url: str) -> str | None:
    """Validate URL for SSRF safety. Returns error message or None if OK."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"Blocked URL scheme: {parsed.scheme!r} (only http/https allowed)"

    hostname = parsed.hostname or ""
    if not hostname:
        return "URL has no hostname"

    # Resolve hostname and check for private/loopback IPs
    # TODO(security): IP is validated at DNS resolution time, not at connection
    # time. If deployment changes from tunneled (e.g. Cloudflare Tunnel) to
    # direct-exposed, implement connection-time IP validation to prevent DNS
    # rebinding attacks. See: security audit report, "DNS rebinding" section.
    try:
        addrinfos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"

    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if _is_private_ip(ip_str):
            return f"Blocked: {hostname} resolves to private/loopback address {ip_str}"

    return None


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates each hop for SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        error = _validate_url(newurl)
        if error:
            raise urllib.error.URLError(f"Redirect blocked: {error}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_safe_opener = urllib.request.build_opener(_SafeRedirectHandler)


async def tool_web_search(query: str, count: int = 10) -> str:
    """Search the web via Brave Search API."""
    if not _search_api_key:
        return "Error: No search API key configured"

    params = urllib.parse.urlencode({"q": query, "count": count})

    if _search_provider == "brave":
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        req = urllib.request.Request(url, headers={  # noqa: S310 — hardcoded https://api.search.brave.com URL
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": _search_api_key,
        })
    else:
        return f"Error: Unknown search provider: {_search_provider}"

    try:
        resp = await asyncio.to_thread(_safe_opener.open, req, timeout=15)
        body = resp.read()
        # Handle gzip
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        return f"Error: Search failed: {e}"

    results = data.get("web", {}).get("results", [])
    if not results:
        return "No results found."

    output = []
    for r in results[:count]:
        title = r.get("title", "")
        url_str = r.get("url", "")
        snippet = r.get("description", "")
        output.append(f"**{title}**\n{url_str}\n{snippet}")

    return "\n\n".join(output)


class _HTMLToText(HTMLParser):
    """Simple HTML→text converter preserving structure."""

    def __init__(self):
        super().__init__()
        self._result: list[str] = []
        self._skip = False
        self._in_pre = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._result.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self._result.append(f" [{href}] ")
        elif tag == "pre":
            self._in_pre = True
            self._result.append("\n```\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False
        elif tag == "pre":
            self._in_pre = False
            self._result.append("\n```\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._result.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            if self._in_pre:
                self._result.append(data)
            else:
                self._result.append(data.strip() + " " if data.strip() else "")

    def get_text(self) -> str:
        text = "".join(self._result)
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


async def tool_web_fetch(url: str, max_chars: int = 50000) -> str:
    """Fetch a URL and convert HTML to readable text."""
    # Validate URL for SSRF
    error = _validate_url(url)
    if error:
        return f"Error: {error}"

    try:
        req = urllib.request.Request(url, headers={  # noqa: S310 — URL validated by _validate_url() (scheme + SSRF); _safe_opener validates redirects
            "User-Agent": "Mozilla/5.0 (compatible; Lucyd/1.0)",
        })
        resp = await asyncio.to_thread(_safe_opener.open, req, timeout=15)
        content_type = resp.headers.get("Content-Type", "")
        body = resp.read()

        # Handle encoding
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()

        text = body.decode(charset, errors="replace")

        # If HTML, convert to text
        if "html" in content_type.lower() or text.strip().startswith("<"):
            parser = _HTMLToText()
            parser.feed(text)
            text = parser.get_text()

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[truncated at {max_chars} chars]"

        return text
    except Exception as e:
        return f"Error fetching {url}: {e}"


TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web. Returns titles, URLs, and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer", "description": "Number of results (default: 10)", "default": 10},
            },
            "required": ["query"],
        },
        "function": tool_web_search,
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL and return its content as readable text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_chars": {"type": "integer", "description": "Max chars to return (default: 50000)", "default": 50000},
            },
            "required": ["url"],
        },
        "function": tool_web_fetch,
    },
]

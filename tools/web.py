"""Web tools — web_search and web_fetch.

web_search: Search via external API (Brave Search by default).
web_fetch: Fetch URL and convert HTML to readable text.
"""

from __future__ import annotations

import asyncio
import gzip
import http.client
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


def _validate_url(url: str) -> tuple[str | None, str | None]:
    """Validate URL for SSRF safety.

    Returns (error_message, resolved_ip).
    error_message is None on success; resolved_ip is None on failure.
    The resolved IP is used by the opener to prevent DNS rebinding.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return f"Invalid URL: {url}", None

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"Blocked URL scheme: {parsed.scheme!r} (only http/https allowed)", None

    hostname = parsed.hostname or ""
    if not hostname:
        return "URL has no hostname", None

    try:
        addrinfos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}", None

    resolved_ip = None
    for _family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        if _is_private_ip(ip_str):
            return f"Blocked: {hostname} resolves to private/loopback address {ip_str}", None
        if resolved_ip is None:
            resolved_ip = ip_str

    return None, resolved_ip


# ─── DNS Rebinding Protection — IP Pinning ─────────────────────
#
# _validate_url resolves DNS once and returns the IP. The request
# carries this IP via attributes. Custom handlers force urllib to
# connect to the validated IP instead of re-resolving, preventing
# DNS rebinding attacks.

_REQ_RESOLVED_IP = "_lucyd_resolved_ip"
_REQ_ORIGINAL_HOST = "_lucyd_original_hostname"


class _IPPinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a pre-resolved IP with correct TLS SNI."""

    def __init__(self, resolved_ip: str, original_hostname: str, **kwargs):
        super().__init__(resolved_ip, **kwargs)
        self._sni_hostname = original_hostname

    def connect(self):
        # TCP connect to the resolved IP
        http.client.HTTPConnection.connect(self)
        # TLS handshake with the original hostname for SNI + cert verification
        server_hostname = self._tunnel_host if self._tunnel_host else self._sni_hostname
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname,
        )


class _IPPinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler that connects to the pre-resolved IP on the request."""

    def http_open(self, req):
        resolved_ip = getattr(req, _REQ_RESOLVED_IP, None)
        if not resolved_ip:
            return super().http_open(req)
        return self.do_open(
            lambda host, **kw: http.client.HTTPConnection(resolved_ip, **kw),
            req,
        )


class _IPPinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that connects to the pre-resolved IP on the request."""

    def https_open(self, req):
        resolved_ip = getattr(req, _REQ_RESOLVED_IP, None)
        original_hostname = getattr(req, _REQ_ORIGINAL_HOST, None)
        if not resolved_ip or not original_hostname:
            return super().https_open(req)
        return self.do_open(
            lambda host, **kw: _IPPinnedHTTPSConnection(
                resolved_ip, original_hostname, **kw,
            ),
            req,
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates each hop for SSRF and pins the new IP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        error, resolved_ip = _validate_url(newurl)
        if error:
            raise urllib.error.URLError(f"Redirect blocked: {error}")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and resolved_ip:
            parsed = urllib.parse.urlparse(newurl)
            setattr(new_req, _REQ_RESOLVED_IP, resolved_ip)
            setattr(new_req, _REQ_ORIGINAL_HOST, parsed.hostname)
        return new_req


_safe_opener = urllib.request.build_opener(
    _IPPinnedHTTPHandler,
    _IPPinnedHTTPSHandler,
    _SafeRedirectHandler,
)


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
    # Validate URL for SSRF — resolve DNS once and pin the IP
    error, resolved_ip = _validate_url(url)
    if error:
        return f"Error: {error}"

    try:
        req = urllib.request.Request(url, headers={  # noqa: S310 — URL validated by _validate_url() (scheme + SSRF); _safe_opener validates redirects
            "User-Agent": "Mozilla/5.0 (compatible; Lucyd/1.0)",
        })
        # Pin connection to the validated IP (prevents DNS rebinding)
        parsed = urllib.parse.urlparse(url)
        setattr(req, _REQ_RESOLVED_IP, resolved_ip)
        setattr(req, _REQ_ORIGINAL_HOST, parsed.hostname)
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

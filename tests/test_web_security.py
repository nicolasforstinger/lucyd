"""Tests for web tool SSRF protection.

Phase 1a: SSRF — tools/web.py
Tests _is_private_ip, _validate_url, _SafeRedirectHandler, tool_web_fetch.
"""

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from tools.web import (
    _REQ_ORIGINAL_HOST,
    _REQ_RESOLVED_IP,
    _is_private_ip,
    _SafeRedirectHandler,
    _validate_url,
    tool_web_fetch,
)

# ─── _is_private_ip — unit tests ────────────────────────────────


class TestIsPrivateIp:
    """Direct IP classification — each class individually tested."""

    def test_loopback_v4(self):
        assert _is_private_ip("127.0.0.1") is True

    def test_loopback_v6(self):
        assert _is_private_ip("::1") is True

    def test_private_10(self):
        assert _is_private_ip("10.0.0.1") is True

    def test_private_172_16(self):
        assert _is_private_ip("172.16.0.1") is True

    def test_private_192_168(self):
        assert _is_private_ip("192.168.1.1") is True

    def test_link_local_cloud_metadata(self):
        """169.254.169.254 — classic cloud metadata endpoint."""
        assert _is_private_ip("169.254.169.254") is True

    def test_link_local_generic(self):
        assert _is_private_ip("169.254.1.1") is True

    def test_reserved_zero(self):
        """0.0.0.0 is reserved."""
        assert _is_private_ip("0.0.0.0") is True

    def test_public_ip(self):
        assert _is_private_ip("8.8.8.8") is False

    def test_ipv6_mapped_v4_loopback(self):
        """::ffff:127.0.0.1 — IPv6-mapped v4 loopback (real bypass vector)."""
        assert _is_private_ip("::ffff:127.0.0.1") is True

    def test_ipv6_mapped_v4_private(self):
        """::ffff:192.168.1.1 — IPv6-mapped v4 private."""
        assert _is_private_ip("::ffff:192.168.1.1") is True

    def test_invalid_ip_blocked(self):
        """Unparseable IP is treated as private (fail closed)."""
        assert _is_private_ip("not-an-ip") is True

    # ─── Boundary tests — edges of private ranges ──────────────

    def test_boundary_10_upper(self):
        """10.255.255.255 — upper edge of 10.0.0.0/8."""
        assert _is_private_ip("10.255.255.255") is True

    def test_boundary_172_16_lower(self):
        """172.16.0.0 — lower edge of 172.16.0.0/12."""
        assert _is_private_ip("172.16.0.0") is True

    def test_boundary_172_31_upper(self):
        """172.31.255.255 — upper edge of 172.16.0.0/12."""
        assert _is_private_ip("172.31.255.255") is True

    def test_boundary_172_15_just_below(self):
        """172.15.255.255 — just below 172.16.0.0/12 (public)."""
        assert _is_private_ip("172.15.255.255") is False

    def test_boundary_172_32_just_above(self):
        """172.32.0.0 — just above 172.16.0.0/12 (public)."""
        assert _is_private_ip("172.32.0.0") is False

    def test_boundary_192_168_upper(self):
        """192.168.255.255 — upper edge of 192.168.0.0/16."""
        assert _is_private_ip("192.168.255.255") is True

    def test_boundary_loopback_upper(self):
        """127.255.255.255 — upper edge of 127.0.0.0/8."""
        assert _is_private_ip("127.255.255.255") is True

    # ─── IP encoding tricks — non-standard notations ─────────

    def test_octal_loopback(self):
        """0177.0.0.1 is octal for 127.0.0.1 — must detect as private."""
        assert _is_private_ip("0177.0.0.1") is True

    def test_hex_loopback(self):
        """0x7f000001 is hex for 127.0.0.1 — must detect as private."""
        assert _is_private_ip("0x7f000001") is True

    def test_decimal_loopback(self):
        """2130706433 is decimal for 127.0.0.1 — must detect as private."""
        assert _is_private_ip("2130706433") is True

    def test_octal_10_network(self):
        """012.0.0.1 is octal for 10.0.0.1 — must detect as private."""
        assert _is_private_ip("012.0.0.1") is True

    def test_hex_10_network(self):
        """0x0a000001 is hex for 10.0.0.1 — must detect as private."""
        assert _is_private_ip("0x0a000001") is True

    def test_hex_public_ip(self):
        """0x08080808 is hex for 8.8.8.8 — must detect as NOT private."""
        assert _is_private_ip("0x08080808") is False

    def test_garbage_string_blocked(self):
        """Nonsense that inet_aton also rejects — must block, not allow."""
        assert _is_private_ip("not-an-ip-at-all") is True

    def test_unparseable_ip_fails_closed(self):
        """Unrecognized IP format must be blocked (fail closed). Unknown = deny."""
        assert _is_private_ip("definitely-not-an-ip") is True

    def test_empty_string_blocked(self):
        """Empty string must be blocked (fail closed)."""
        assert _is_private_ip("") is True


# ─── _validate_url — unit tests ─────────────────────────────────


class TestValidateUrl:
    """URL validation pipeline for SSRF prevention.

    _validate_url returns (error, resolved_ip).
    """

    def test_https_allowed(self):
        err, ip = _validate_url("https://example.com")
        assert err is None
        assert ip is not None

    def test_http_allowed(self):
        err, ip = _validate_url("http://example.com")
        assert err is None
        assert ip is not None

    def test_blocks_file_scheme(self):
        """file:///etc/passwd — local file read."""
        err, ip = _validate_url("file:///etc/passwd")
        assert err is not None
        assert "Blocked URL scheme" in err
        assert ip is None

    def test_blocks_ftp_scheme(self):
        err, ip = _validate_url("ftp://evil.com/payload")
        assert err is not None
        assert "Blocked URL scheme" in err

    def test_blocks_data_scheme(self):
        err, ip = _validate_url("data:text/html,<h1>test</h1>")
        assert err is not None
        assert "Blocked URL scheme" in err

    def test_blocks_empty_hostname(self):
        """http:///path — empty hostname."""
        err, ip = _validate_url("http:///path")
        assert err is not None

    def test_blocks_no_hostname(self):
        err, ip = _validate_url("http://")
        assert err is not None

    def test_blocks_private_ip_192_168(self):
        err, ip = _validate_url("http://192.168.1.1/")
        assert err is not None
        assert "private" in err.lower() or "loopback" in err.lower()

    def test_blocks_localhost(self):
        err, ip = _validate_url("http://localhost:8080/api")
        assert err is not None

    def test_blocks_127_0_0_1(self):
        err, ip = _validate_url("http://127.0.0.1:8080/api")
        assert err is not None

    def test_blocks_10_network(self):
        err, ip = _validate_url("http://10.0.0.1/secret")
        assert err is not None

    def test_blocks_172_16_network(self):
        err, ip = _validate_url("http://172.16.0.1/internal")
        assert err is not None

    def test_blocks_hex_ip_loopback(self):
        """http://0x7f000001/ — hex encoding of 127.0.0.1. Must block as private."""
        err, ip = _validate_url("http://0x7f000001/")
        assert err is not None
        assert "private" in err.lower() or "loopback" in err.lower()

    def test_blocks_decimal_ip_loopback(self):
        """http://2130706433/ — decimal encoding of 127.0.0.1. Must block as private."""
        err, ip = _validate_url("http://2130706433/")
        assert err is not None
        assert "private" in err.lower() or "loopback" in err.lower()

    def test_blocks_octal_ip_loopback(self):
        """http://0177.0.0.1/ — octal encoding of 127.0.0.1. Must block as private."""
        err, ip = _validate_url("http://0177.0.0.1/")
        assert err is not None
        assert "private" in err.lower() or "loopback" in err.lower()

    def test_blocks_unresolvable_hostname(self):
        """Hostname that can't be resolved should be blocked."""
        err, ip = _validate_url("http://this-domain-does-not-exist-xyzzy.example/")
        assert err is not None

    def test_blocks_javascript_scheme(self):
        err, ip = _validate_url("javascript:alert(1)")
        assert err is not None
        assert "Blocked URL scheme" in err

    def test_blocks_gopher_scheme(self):
        err, ip = _validate_url("gopher://evil.com/")
        assert err is not None
        assert "Blocked URL scheme" in err

    def test_blocks_dict_scheme(self):
        err, ip = _validate_url("dict://evil.com/")
        assert err is not None
        assert "Blocked URL scheme" in err

    def test_blocks_empty_scheme(self):
        err, ip = _validate_url("://example.com/")
        assert err is not None
        assert "Blocked URL scheme" in err

    # ─── Mutation-targeted tests ─────────────────────────────────

    def test_scheme_error_includes_scheme_repr(self):
        """Error must include the repr'd scheme — kills {parsed.scheme!r} removal."""
        err, ip = _validate_url("ftp://evil.com/")
        assert "'ftp'" in err

    def test_bare_hostname_no_scheme(self):
        """URL with no scheme at all → parsed.scheme is empty → blocked."""
        err, ip = _validate_url("example.com/path")
        assert err is not None

    def test_empty_hostname_error_says_no_hostname(self):
        """Empty hostname must say 'no hostname', not a DNS error."""
        err, ip = _validate_url("http:///path")
        assert "no hostname" in err.lower()

    def test_dns_error_includes_hostname(self):
        """DNS failure error must include the hostname that failed."""
        err, ip = _validate_url("http://this-domain-does-not-exist-xyzzy.example/")
        assert "this-domain-does-not-exist-xyzzy.example" in err

    def test_private_ip_error_includes_ip_address(self):
        """Private IP error must include the actual resolved IP."""
        err, ip = _validate_url("http://192.168.1.1/")
        assert "192.168.1.1" in err

    def test_default_port_443_when_no_port(self):
        """When URL has no explicit port, getaddrinfo gets port=443."""
        import socket as _socket
        fake = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake) as mock_gai:
            err, ip = _validate_url("https://example.com/page")
            assert err is None
            assert ip == "93.184.216.34"
            assert mock_gai.call_args[0][1] == 443

    def test_explicit_port_passthrough(self):
        """Explicit port in URL must be passed to getaddrinfo, not overridden."""
        import socket as _socket
        fake = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8080))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake) as mock_gai:
            err, ip = _validate_url("https://example.com:8080/page")
            assert err is None
            assert ip == "93.184.216.34"
            assert mock_gai.call_args[0][1] == 8080

    def test_getaddrinfo_uses_ipproto_tcp(self):
        """getaddrinfo must filter by IPPROTO_TCP."""
        import socket as _socket
        fake = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake) as mock_gai:
            _validate_url("https://example.com/")
            assert mock_gai.call_args[1].get("proto") == _socket.IPPROTO_TCP

    def test_returns_resolved_ip(self):
        """Successful validation returns the resolved IP address."""
        import socket as _socket
        fake = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake):
            err, ip = _validate_url("https://example.com/")
            assert err is None
            assert ip == "93.184.216.34"

    def test_blocked_ip_returns_none_ip(self):
        """Blocked URL returns None as resolved_ip."""
        err, ip = _validate_url("http://127.0.0.1/")
        assert err is not None
        assert ip is None


# ─── _SafeRedirectHandler — unit tests ──────────────────────────


class TestSafeRedirectHandler:
    """Redirect-based SSRF defense. Tests call the REAL handler method."""

    def _make_handler(self):
        """Create a _SafeRedirectHandler instance."""
        return _SafeRedirectHandler()

    def _make_request(self, url="http://example.com"):
        return urllib.request.Request(url)

    def test_redirect_to_cloud_metadata_blocked(self):
        """Redirect to 169.254.169.254 (classic SSRF to cloud metadata) — MUST block."""
        handler = self._make_handler()
        req = self._make_request()
        with pytest.raises(urllib.error.URLError, match="Redirect blocked"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
            )

    def test_redirect_to_loopback_blocked(self):
        """Redirect to 127.0.0.1 — MUST block."""
        handler = self._make_handler()
        req = self._make_request()
        with pytest.raises(urllib.error.URLError, match="Redirect blocked"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://127.0.0.1:8080/"
            )

    def test_redirect_to_private_10_blocked(self):
        handler = self._make_handler()
        req = self._make_request()
        with pytest.raises(urllib.error.URLError, match="Redirect blocked"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://10.0.0.1/internal"
            )

    def test_redirect_to_public_url_allowed(self):
        """Redirect to a public URL should NOT raise (allowed through)."""
        import socket
        handler = self._make_handler()
        req = self._make_request("http://example.com")
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake_addrinfo):
            try:
                handler.redirect_request(
                    req, None, 302, "Found", {}, "https://other-public.com/"
                )
            except urllib.error.URLError:
                pytest.fail("Public URL redirect should not be blocked")

    def test_redirect_pins_resolved_ip(self):
        """Redirect to a public URL carries the resolved IP on the new request."""
        import socket
        handler = self._make_handler()
        req = self._make_request("http://example.com")
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake_addrinfo):
            new_req = handler.redirect_request(
                req, None, 302, "Found", {}, "https://other-public.com/page"
            )
            assert getattr(new_req, _REQ_RESOLVED_IP) == "93.184.216.34"
            assert getattr(new_req, _REQ_ORIGINAL_HOST) == "other-public.com"

    def test_redirect_to_file_scheme_blocked(self):
        """Redirect to file:// — MUST block."""
        handler = self._make_handler()
        req = self._make_request()
        with pytest.raises(urllib.error.URLError, match="Redirect blocked"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "file:///etc/passwd"
            )


# ─── tool_web_fetch — integration tests ─────────────────────────


class TestToolWebFetchIntegration:
    """Integration: tool_web_fetch calls _validate_url BEFORE fetching."""

    @pytest.mark.asyncio
    async def test_private_ip_rejected(self):
        """tool_web_fetch('http://192.168.1.1/') → error."""
        result = await tool_web_fetch("http://192.168.1.1/")
        assert "Error" in result
        assert "private" in result.lower() or "loopback" in result.lower() or "Blocked" in result

    @pytest.mark.asyncio
    async def test_file_scheme_rejected(self):
        """tool_web_fetch('file:///etc/passwd') → error."""
        result = await tool_web_fetch("file:///etc/passwd")
        assert "Error" in result
        assert "Blocked URL scheme" in result

    @pytest.mark.asyncio
    async def test_loopback_rejected(self):
        result = await tool_web_fetch("http://127.0.0.1:8080/")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_valid_url_calls_opener(self):
        """Valid URL passes validation and attempts to open."""
        with patch("tools.web._safe_opener") as mock_opener:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"<html><body>Hello</body></html>"
            mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
            mock_opener.open.return_value = mock_resp
            result = await tool_web_fetch("http://example.com/page")
            assert "Hello" in result
            mock_opener.open.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_pins_resolved_ip_on_request(self):
        """tool_web_fetch sets resolved IP attributes on the urllib Request."""
        import socket as _socket
        fake = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        with patch("tools.web.socket.getaddrinfo", return_value=fake), \
             patch("tools.web._safe_opener") as mock_opener:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"ok"
            mock_resp.headers = {"Content-Type": "text/plain"}
            mock_opener.open.return_value = mock_resp
            await tool_web_fetch("http://example.com/")
            # Verify the Request passed to opener has the pinned IP
            req = mock_opener.open.call_args[0][0]
            assert getattr(req, _REQ_RESOLVED_IP) == "93.184.216.34"
            assert getattr(req, _REQ_ORIGINAL_HOST) == "example.com"


# ─── _HTMLToText ──────────────────────────────────────────────────


from tools.web import _HTMLToText


class TestHTMLToText:
    """Unit tests for HTML→text conversion."""

    def test_skips_script_content(self):
        p = _HTMLToText()
        p.feed("<div>visible</div><script>var x = 1;</script><div>also</div>")
        assert "var x" not in p.get_text()
        assert "visible" in p.get_text()

    def test_skips_style_content(self):
        p = _HTMLToText()
        p.feed("<style>body { color: red; }</style><p>Hello</p>")
        assert "color: red" not in p.get_text()
        assert "Hello" in p.get_text()

    def test_skips_noscript_content(self):
        p = _HTMLToText()
        p.feed("<noscript>Enable JS</noscript><p>Content</p>")
        assert "Enable JS" not in p.get_text()
        assert "Content" in p.get_text()

    def test_extracts_link_urls(self):
        p = _HTMLToText()
        p.feed('<a href="http://example.com">click</a>')
        text = p.get_text()
        assert "[http://example.com]" in text
        assert "click" in text

    def test_preserves_pre_blocks(self):
        p = _HTMLToText()
        p.feed("<pre>def foo():\n    pass</pre>")
        text = p.get_text()
        assert "```" in text
        assert "def foo():" in text

    def test_block_elements_add_newlines(self):
        p = _HTMLToText()
        p.feed("<h1>Title</h1><p>Para one.</p><p>Para two.</p>")
        text = p.get_text()
        assert "Title" in text
        assert "Para one." in text
        assert "Para two." in text
        # Should have line breaks between block elements
        lines = [l for l in text.split("\n") if l.strip()]
        assert len(lines) >= 3

    def test_collapses_excessive_newlines(self):
        p = _HTMLToText()
        p.feed("<div></div><div></div><div></div><div></div><div>text</div>")
        assert "\n\n\n" not in p.get_text()

    def test_strips_outer_whitespace(self):
        p = _HTMLToText()
        p.feed("  <p>hello</p>  ")
        text = p.get_text()
        assert text == text.strip()

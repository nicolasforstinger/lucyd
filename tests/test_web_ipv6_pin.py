"""Regression: IPv6 pinned connections must bracket the literal.

The SSRF defense resolves DNS once and pins the connection to the resulting
IP. For an IPv6 literal, http.client.HTTPConnection splits host:port on the
last colon unless the address is bracketed — so an unbracketed IPv6 pin
mis-parses (host truncated, a hextet read as the port) and never connects.
"""

from __future__ import annotations

from tools import web


def test_conn_host_brackets_ipv6_only() -> None:
    assert web._conn_host("2606:4700:4700::1111") == "[2606:4700:4700::1111]"
    assert web._conn_host("93.184.216.34") == "93.184.216.34"


def test_ipv6_pinned_https_connection_parses_host_and_port() -> None:
    conn = web._IPPinnedHTTPSConnection("2606:4700:4700::1111", "example.com")
    assert conn.host == "2606:4700:4700::1111"
    assert conn.port == 443  # default HTTPS port — not a hextet mis-read as the port

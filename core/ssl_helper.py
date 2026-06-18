"""
ssl_helper.py — Shared SSL + connector factory
=================================================
Mengganti ssl=False di semua module dengan proper SSL context.
Tetap support custom DNS resolver untuk bypass ISP blocks.
"""
import ssl
import logging
import os
import socket
from aiohttp import TCPConnector

log = logging.getLogger("ssl_helper")
_py_clob_ssl_configured = False
_dns_fallback_installed = False
_original_getaddrinfo = socket.getaddrinfo
_dns_fallback_logged_hosts: set[str] = set()


def _resolved_ipv4(host: str) -> list[str]:
    return sorted({
        str(row[4][0])
        for row in _original_getaddrinfo(
            host,
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    })


def install_polymarket_dns_fallback() -> None:
    """Route poisoned Polymarket DNS answers through a valid Cloudflare edge."""
    global _dns_fallback_installed
    if _dns_fallback_installed:
        return
    enabled = os.getenv(
        "POLYMARKET_DNS_FALLBACK_ENABLED", "true"
    ).strip().lower() in {"true", "1", "yes", "on"}
    if not enabled:
        return

    try:
        edge_ips = _resolved_ipv4("cloudflare-dns.com")
    except OSError:
        edge_ips = ["104.16.248.249", "104.16.249.249"]
    if not edge_ips:
        return

    def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        result = _original_getaddrinfo(host, port, family, type, proto, flags)
        host_text = host.decode() if isinstance(host, bytes) else str(host or "")
        if not host_text.lower().endswith(".polymarket.com"):
            return result
        resolved = {str(row[4][0]) for row in result}
        if not resolved or not all(ip.startswith("114.7.173.") for ip in resolved):
            return result

        fallback = []
        for edge_ip in edge_ips:
            fallback.extend(_original_getaddrinfo(
                edge_ip,
                port,
                socket.AF_INET,
                type,
                proto,
                flags,
            ))
        if host_text not in _dns_fallback_logged_hosts:
            _dns_fallback_logged_hosts.add(host_text)
            log.warning(
                "DNS: blocked answer %s for %s; using Cloudflare edge %s",
                ",".join(sorted(resolved)),
                host_text,
                ",".join(edge_ips),
            )
        return fallback

    socket.getaddrinfo = _getaddrinfo
    _dns_fallback_installed = True


def make_ssl_context() -> ssl.SSLContext:
    """
    Buat SSL context yang aman.
    Muat Windows system trust lebih dulu, lalu tambahkan certifi.

    Browser Windows dapat mempercayai CA lokal yang tidak ada di bundle
    certifi. Menggabungkan keduanya menjaga verifikasi TLS tetap aktif.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        log.debug("SSL: using Windows system trust + certifi")
    except ImportError:
        log.debug("SSL: using Windows system trust (certifi not installed)")
    return ctx


def configure_py_clob_httpx() -> bool:
    """Replace the SDK global HTTP client with the combined trust context."""
    global _py_clob_ssl_configured
    if _py_clob_ssl_configured:
        return True

    try:
        import httpx
        from py_clob_client_v2.http_helpers import helpers

        install_polymarket_dns_fallback()
        ssl_ctx = make_ssl_context()
        transport = httpx.HTTPTransport(
            verify=ssl_ctx,
            http2=True,
            retries=2,
        )
        old_client = getattr(helpers, "_http_client", None)
        helpers._http_client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(10.0, connect=10.0),
        )
        _py_clob_ssl_configured = True
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
        log.info("SSL: py_clob_client_v2 uses Windows system trust + certifi")
        return True
    except Exception as exc:
        log.error("SSL: failed to configure py_clob_client_v2 trust: %s", exc)
        return False


def make_connector(use_custom_dns: bool = True) -> TCPConnector:
    """
    Buat TCPConnector dengan SSL aktif + custom DNS resolver.
    Drop-in replacement untuk semua TCPConnector(ssl=False).
    """
    install_polymarket_dns_fallback()
    ssl_ctx = make_ssl_context()

    custom_dns_enabled = os.getenv(
        "CUSTOM_DNS_ENABLED", "false"
    ).strip().lower() in {"true", "1", "yes", "on"}
    if use_custom_dns and custom_dns_enabled:
        try:
            from aiohttp.resolver import AsyncResolver
            resolver = AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
            return TCPConnector(resolver=resolver, ssl=ssl_ctx)
        except Exception as e:
            log.debug("Custom DNS resolver failed: %s — using default", e)

    from aiohttp.resolver import ThreadedResolver
    return TCPConnector(resolver=ThreadedResolver(), ssl=ssl_ctx)

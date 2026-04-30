"""Connectivity probing for modem setup.

Detects which protocol a modem speaks (HTTP vs HTTPS), whether it
requires legacy SSL ciphers, and which health-monitoring probes it
supports.  All functions are synchronous — the HA adapter wraps them
in ``hass.async_add_executor_job()``.

These probes run **once** during config-flow validation.  The results
are persisted in the HA config entry and reused at every poll — the
runtime path never re-discovers protocol or probe support.

See CONFIG_FLOW_SPEC.md § Step 4 for the validation pipeline.
"""

from __future__ import annotations

import logging
import socket
import ssl
import subprocess
from dataclasses import dataclass
from typing import Any

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.ssl_ import create_urllib3_context

# Suppress InsecureRequestWarning globally.
# Cable modems use self-signed certs on private LANs; we always use
# verify=False.  The warning is noise, not a signal.
urllib3.disable_warnings(InsecureRequestWarning)


class _HNAPHeaderParsingFilter(logging.Filter):
    """Suppress urllib3 "Failed to parse headers" warnings.

    Some HNAP modems send malformed HTTP headers with debug timing
    data prepended to header values. urllib3 emits a warning for each,
    producing noisy log entries on every poll. This filter drops those
    records at the logging level so they never reach any handler.

    See RESOURCE_LOADING_SPEC.md § HNAP Header Parsing Warning Suppression.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False to drop records containing the header parse warning."""
        return "Failed to parse headers" not in record.getMessage()


# Apply the filter once at import time — safe for all modems since
# standard header parsing warnings are infrastructure noise.
logging.getLogger("urllib3.connectionpool").addFilter(
    _HNAPHeaderParsingFilter(),
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy SSL support
# ---------------------------------------------------------------------------

# Cipher string that allows legacy algorithms while preferring modern ones.
# SECLEVEL=0 disables OpenSSL security-level checks, permitting legacy
# ciphers (3DES, RC4) that older modem firmware may require.
LEGACY_CIPHERS = "DEFAULT:@SECLEVEL=0"

_DEFAULT_TIMEOUT = 5.0


class LegacySSLAdapter(HTTPAdapter):
    """``HTTPAdapter`` that allows legacy SSL ciphers.

    Older modem firmware (e.g., early Arris S33 builds) only supports
    TLS cipher suites that Python 3.10+ rejects by default.  Mounting
    this adapter on ``https://`` downgrades the cipher floor so those
    devices remain reachable.

    This is acceptable for local LAN devices with self-signed
    certificates.  Not recommended for public internet.

    Usage::

        session = requests.Session()
        session.mount("https://", LegacySSLAdapter())
        response = session.get("https://192.168.100.1", verify=False)
    """

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Create pool manager with legacy SSL context."""
        context = create_urllib3_context(ciphers=LEGACY_CIPHERS)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        """Create proxy manager with legacy SSL context."""
        context = create_urllib3_context(ciphers=LEGACY_CIPHERS)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        proxy_kwargs["ssl_context"] = context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def create_session(*, legacy_ssl: bool = False) -> requests.Session:
    """Create a ``requests.Session`` with appropriate SSL handling.

    Args:
        legacy_ssl: Mount :class:`LegacySSLAdapter` for HTTPS URLs.

    Returns:
        Configured session with ``verify=False``.
    """
    session = requests.Session()
    session.verify = False
    if legacy_ssl:
        session.mount("https://", LegacySSLAdapter())
    return session


# ---------------------------------------------------------------------------
# Protocol detection
# ---------------------------------------------------------------------------

# TLS protocol versions classed as "legacy" — the modem's stack is old
# enough that runtime sessions need ``LegacySSLAdapter`` mounted.
# ``sock.version()`` returns one of these literal strings.
_LEGACY_TLS_VERSIONS = frozenset({"SSLv2", "SSLv3", "TLSv1", "TLSv1.1"})


@dataclass
class ConnectivityResult:
    """Result of :func:`detect_protocol`.

    Attributes:
        success: True if the modem responded to at least one probe.
        protocol: ``"http"`` or ``"https"`` — whichever worked first.
        legacy_ssl: True if the modem negotiated TLS 1.1 or older —
            runtime callers must mount :class:`LegacySSLAdapter` so
            HTTPS sessions allow the same ciphers.
        working_url: Full URL that responded (e.g. ``https://192.168.100.1``).
        error: Human-readable message when ``success`` is False.
    """

    success: bool
    protocol: str | None = None
    legacy_ssl: bool = False
    working_url: str | None = None
    error: str | None = None


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    """Return True if ``host:port`` accepts a TCP connection.

    Pinned to IPv4 — most consumer cable modems are IPv4-only on the
    LAN side, and a dual-stack ``getaddrinfo`` may otherwise return
    IPv6 first and false-fail the probe before falling back to v4.
    """
    try:
        infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as exc:
        _logger.debug("TCP probe %s:%d — address resolution failed: %s", host, port, exc)
        return False

    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return True
        except (OSError, TimeoutError) as exc:
            _logger.debug("TCP probe %s:%d failed: %s", host, port, exc)
        finally:
            sock.close()
    return False


def _tls_handshake(host: str, port: int, timeout: float) -> tuple[bool, bool]:
    """Open a TLS connection with broad ciphers and report what got negotiated.

    Uses a ``SECLEVEL=0`` cipher context so the handshake succeeds
    regardless of the modem's TLS age — the runtime adapter decision
    is driven by the *negotiated* protocol version, not by what we
    tried first.

    Returns:
        ``(handshake_ok, legacy_negotiated)``. ``legacy_negotiated``
        is True when the modem chose TLS 1.1 or older.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers(LEGACY_CIPHERS)

    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw_sock,
            context.wrap_socket(raw_sock, server_hostname=host) as tls_sock,
        ):
            version = tls_sock.version() or ""
            legacy = version in _LEGACY_TLS_VERSIONS
            _logger.info(
                "TLS probe %s:%d — negotiated %s%s",
                host,
                port,
                version or "unknown",
                " (legacy)" if legacy else "",
            )
            return (True, legacy)
    except (ssl.SSLError, OSError, TimeoutError) as exc:
        _logger.debug("TLS handshake to %s:%d failed: %s", host, port, exc)
        return (False, False)


def _strip_protocol(host: str) -> tuple[str | None, str]:
    """Split an optional protocol prefix from a host string.

    Returns ``(protocol, bare_host)``. ``protocol`` is ``"http"`` or
    ``"https"`` if the input had a prefix, else None. ``bare_host``
    may still include a ``:port`` suffix.
    """
    for prefix, name in (("http://", "http"), ("https://", "https")):
        if host.startswith(prefix):
            bare = host[len(prefix) :].split("/", 1)[0]
            return (name, bare)
    return (None, host.split("/", 1)[0])


def _split_host_port(host: str) -> tuple[str, int | None]:
    """Split ``host:port`` into ``(hostname, port)``.

    Returns ``(host, None)`` when the input has no port suffix.
    Bracketed IPv6 literals (``[::1]:8080``) are handled — the
    bracket form is the only safe way to disambiguate IPv6 from a
    bare ``host:port`` colon.
    """
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return (host, None)
        hostname = host[1:end]
        tail = host[end + 1 :]
        if tail.startswith(":") and tail[1:].isdigit():
            return (hostname, int(tail[1:]))
        return (hostname, None)
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if tail.isdigit():
            return (head, int(tail))
    return (host, None)


def detect_protocol(
    host: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ConnectivityResult:
    """Detect the working protocol for a modem.

    Probes TCP ``:80`` and ``:443``. If ``:443`` accepts connections
    *and* completes a TLS handshake, prefers HTTPS — modems that
    expose both ports almost always intend HTTPS for authenticated
    traffic, and ``:80`` is typically a redirect or legacy stub.

    Sets ``legacy_ssl`` from the negotiated TLS protocol version
    rather than from a failed-and-retried-with-weaker-ciphers
    inference. The runtime ``LegacySSLAdapter`` decision is driven
    by what the modem actually picked.

    If *host* already includes a protocol prefix (``http://`` or
    ``https://``), only that transport is probed — the user has
    explicitly chosen.

    Args:
        host: IP address, hostname, or full URL.
        timeout: Per-probe timeout in seconds (applied to each TCP
            probe and the TLS handshake separately).

    Returns:
        :class:`ConnectivityResult` describing the chosen transport.
    """
    explicit_protocol, bare_host = _strip_protocol(host)
    hostname, port_override = _split_host_port(bare_host)
    http_port = port_override or 80
    https_port = port_override or 443
    url_host = bare_host  # preserves any user-supplied :port

    _logger.info(
        "Protocol detection: probing %s%s",
        url_host,
        f" (user-specified {explicit_protocol})" if explicit_protocol else "",
    )

    http_open = False
    if explicit_protocol in (None, "http"):
        http_open = _tcp_probe(hostname, http_port, timeout)

    https_open = False
    legacy_ssl = False
    if explicit_protocol in (None, "https") and _tcp_probe(hostname, https_port, timeout):
        https_open, legacy_ssl = _tls_handshake(hostname, https_port, timeout)

    if https_open:
        url = f"https://{url_host}"
        _logger.info(
            "Protocol detection: HTTPS reachable%s — using %s",
            " (legacy TLS)" if legacy_ssl else "",
            url,
        )
        return ConnectivityResult(
            success=True,
            protocol="https",
            legacy_ssl=legacy_ssl,
            working_url=url,
        )
    if http_open:
        url = f"http://{url_host}"
        _logger.info("Protocol detection: HTTP reachable — using %s", url)
        return ConnectivityResult(
            success=True,
            protocol="http",
            working_url=url,
        )

    tried = (
        f"TCP {hostname}:{http_port}"
        if explicit_protocol == "http"
        else (
            f"TCP {hostname}:{https_port} (TLS handshake)"
            if explicit_protocol == "https"
            else f"TCP {hostname}:{http_port} and {hostname}:{https_port}"
        )
    )
    return ConnectivityResult(
        success=False,
        error=f"Cannot connect to modem at {url_host}. Tried: {tried}.",
    )


# ---------------------------------------------------------------------------
# Health-probe discovery
# ---------------------------------------------------------------------------


def test_icmp(host: str, *, timeout: int = 2) -> bool:
    """Test whether the host responds to ICMP echo (ping).

    Runs the system ``ping`` command.  Returns False if ICMP is
    blocked, the host is unreachable, or the command is unavailable.

    Args:
        host: IP address or hostname.
        timeout: Wait time in seconds (``ping -W``).
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            capture_output=True,
            timeout=timeout + 2,
            check=False,
        )
        ok = result.returncode == 0
        _logger.info("ICMP probe %s: %s", host, "ok" if ok else "blocked/timeout")
        return ok
    except Exception as exc:
        _logger.debug("ICMP probe %s: %s", host, exc)
        return False


def test_http_head(
    url: str,
    *,
    legacy_ssl: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """Test whether the modem responds to HTTP HEAD requests.

    Some modems (e.g., Technicolor TC4400 with micro_httpd) reject
    HEAD — they only respond to GET.  This probe lets the health
    monitor know whether HEAD is usable.

    Args:
        url: Full URL (e.g. ``http://192.168.100.1``).
        legacy_ssl: Use ``SECLEVEL=0`` ciphers for HTTPS.
        timeout: Request timeout in seconds.

    Returns:
        True if HEAD returns a status < 500.
    """
    try:
        session = create_session(legacy_ssl=legacy_ssl)
        resp = session.head(url, timeout=timeout, allow_redirects=False)
        ok = resp.status_code < 500
        if ok:
            _logger.info("HEAD probe %s: supported (%d)", url, resp.status_code)
        else:
            _logger.info("HEAD probe %s: not supported (%d), health checks will use GET", url, resp.status_code)
        return ok
    except (requests.RequestException, OSError):
        _logger.info("HEAD probe %s: not supported, health checks will use GET", url)
        return False

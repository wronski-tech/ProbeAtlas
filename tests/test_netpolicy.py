"""SSRF protections: address classification, hostname rules, redirects, ports."""

from __future__ import annotations

import pytest

from probeatlas.common.netpolicy import BlockedTargetError, NetworkPolicy, resolve_checked, validate_hostname
from probeatlas.common.safehttp import http_request, validate_http_url
from probeatlas.common.schemas import FailureCategory

STRICT = NetworkPolicy()


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.1.1", "169.254.169.254", "169.254.1.1", "0.0.0.0",
        "100.64.0.1", "224.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1", "::",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "64:ff9b::a9fe:a9fe",  # NAT64-embedded 169.254.169.254
        "2002:7f00:1::",  # 6to4-embedded 127.0.0.1
        "fd00:ec2::254",
        "::7f00:1",  # IPv4-compatible 127.0.0.1
        "::a9fe:a9fe",  # IPv4-compatible 169.254.169.254
        "::ffff:0:7f00:1",  # SIIT-translated 127.0.0.1
        "64:ff9b:1::7f00:1",  # local-use NAT64 (RFC 8215)
        "fec0::1",  # deprecated site-local
    ],
)
def test_non_public_addresses_blocked(ip):
    assert STRICT.ip_block_reason(__import__("ipaddress").ip_address(ip)) is not None


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    STRICT.check_ip(ip)


def test_allowlisted_private_cidr_never_unblocks_metadata():
    policy = NetworkPolicy.from_cidrs("169.254.0.0/16,10.0.0.0/8")
    policy.check_ip("10.9.8.7")
    policy.check_ip("169.254.10.10")
    with pytest.raises(BlockedTargetError, match="metadata"):
        policy.check_ip("169.254.169.254")
    with pytest.raises(BlockedTargetError):
        policy.check_ip("192.168.0.1")


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST.", "db.internal", "printer.local", "metadata.google.internal", "intranet",
             "foo.localhost", "127.0.0.1", "[::1]"],
)
def test_local_hostnames_rejected(host):
    with pytest.raises(BlockedTargetError):
        validate_hostname(host, STRICT)


@pytest.mark.parametrize("host", ["bad_host.com", "-bad.com", "a..b.com", "x" * 64 + ".com", "1.2.3"])
def test_malformed_hostnames_rejected(host):
    with pytest.raises((ValueError, BlockedTargetError)):
        validate_hostname(host, STRICT)


def test_hostname_normalization():
    assert validate_hostname("Example.COM.", STRICT) == "example.com"
    assert validate_hostname("bücher.de", STRICT) == "xn--bcher-kva.de"
    assert validate_hostname("2606:4700:4700:0::1111", STRICT) == "2606:4700:4700::1111"


@pytest.mark.parametrize(
    "url,err",
    [
        ("file:///etc/passwd", ValueError),
        ("gopher://example.com/", ValueError),
        ("http://user:pass@example.com/", ValueError),
        ("http://example.com:22/", BlockedTargetError),
        ("http://example.com:25/", BlockedTargetError),
        ("http://169.254.169.254/latest/meta-data", BlockedTargetError),
        ("http://[::ffff:10.0.0.1]/", BlockedTargetError),
    ],
)
def test_url_validation(url, err):
    with pytest.raises(err):
        validate_http_url(url, STRICT)


def test_url_normalization_keeps_path_and_query():
    assert validate_http_url("HTTPS://Example.com:8443/a/b?x=1", STRICT) == "https://example.com:8443/a/b?x=1"
    assert validate_http_url("http://example.com", STRICT) == "http://example.com/"


def test_resolution_time_validation_blocks_names_pointing_at_private_addresses():
    # Syntactic checks relaxed, but the resolved address (loopback) is still rejected:
    # this is the layer that defeats DNS names pointing at internal addresses.
    relaxed = NetworkPolicy(allow_private_hostnames=True)
    with pytest.raises(BlockedTargetError, match="loopback"):
        resolve_checked("localhost", 80, relaxed, timeout_s=2)


def test_http_request_to_private_address_is_blocked_before_connecting(target_server):
    out = http_request(target_server.url("/ok"), policy=STRICT, timeout_s=2)
    assert out.error and out.error.category == FailureCategory.BLOCKED_TARGET
    assert target_server.requests == []


@pytest.mark.parametrize(
    "dest",
    ["http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://[::1]:8080/", "http://localhost/",
     "file:///etc/passwd"],
)
def test_redirects_to_prohibited_destinations_are_not_followed(target_server, dest):
    # Allow only the test server's address so the first hop succeeds.
    policy = NetworkPolicy.from_cidrs("127.0.0.1/32")
    out = http_request(target_server.url(f"/redirect?to={dest}"), policy=policy, timeout_s=3)
    assert out.error is not None
    assert out.error.category in (FailureCategory.BLOCKED_TARGET, FailureCategory.PROTOCOL_ERROR)
    assert "redirect rejected" in out.error.message
    assert len(target_server.requests) == 1


def test_redirect_limit(target_server):
    policy = NetworkPolicy.from_cidrs("127.0.0.1/32")
    loop = target_server.url("/redirect?to=/redirect%3Fto%3D/ok")
    ok = http_request(loop, policy=policy, timeout_s=3, max_redirects=2)
    assert ok.error is None and ok.status == 200 and ok.redirects == 2
    limited = http_request(loop, policy=policy, timeout_s=3, max_redirects=1)
    assert limited.error.category == FailureCategory.REDIRECT_LIMIT

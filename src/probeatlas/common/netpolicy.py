"""Outbound network policy shared by the agent and the control plane.

Monitoring agents (and webhook delivery in the control plane) make outbound
connections to customer-supplied destinations. This module is the single place
that decides whether a hostname or IP address may be contacted.

Enforcement happens in two layers:

1. Syntactic validation when a monitor is created (``validate_hostname``).
2. Resolution-time validation immediately before connecting
   (``resolve_checked``). Callers connect to the exact addresses returned here,
   so a DNS answer cannot change between the check and the connection
   (DNS rebinding).

Private, loopback, link-local, multicast, reserved, and cloud metadata
addresses are blocked unless an operator explicitly allowlists a private CIDR.
Cloud metadata endpoints are blocked even when allowlisted.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
from dataclasses import dataclass, field

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Cloud provider instance metadata endpoints. Blocked unconditionally.
METADATA_ADDRESSES: frozenset[IPAddress] = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254",  # AWS, GCP, Azure, DigitalOcean, OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "169.254.169.253",  # AWS DNS resolver
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IPv6 metadata
    )
)

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".corp",
)

_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")  # RFC 6052/7915 IPv4-translated
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
# Non-public IPv6 ranges that some Python versions classify as global.
_EXTRA_BLOCKED = (
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64 (RFC 8215)
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
)

# getaddrinfo() has no timeout parameter; resolve in a pool so a hung resolver cannot block a check beyond
# its deadline. Abandoned lookups keep their thread until the system resolver gives up (several seconds), so
# the pool is sized well above agent concurrency: a tenant pointing monitors at unresponsive nameservers
# must not starve other tenants' lookups. Replacing getaddrinfo with dnspython (hard lifetime) is tracked in
# docs/security-review.md.
_resolver_pool = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="resolve")


class BlockedTargetError(Exception):
    """The destination is not permitted by the outbound network policy."""


class ResolutionError(Exception):
    """DNS resolution failed before any policy decision could be made.

    ``nxdomain`` is true when the name does not exist and ``timeout`` when the resolver did not answer in
    time, so callers can map the failure to a precise :class:`FailureCategory`.
    """

    def __init__(self, message: str, *, nxdomain: bool = False, timeout: bool = False):
        """Store the message and the ``nxdomain`` / ``timeout`` flags."""
        super().__init__(message)
        self.nxdomain = nxdomain
        self.timeout = timeout


@dataclass(frozen=True)
class NetworkPolicy:
    """Which destinations may be contacted.

    ``allowed_private_networks`` is an explicit operator opt-in for private
    monitoring (for example a local demo target network). It never unblocks
    cloud metadata addresses.

    ``allow_private_hostnames`` relaxes the syntactic hostname checks
    (``localhost``, single-label names, ``.internal``). Resolution-time IP
    checks still apply.
    """

    allowed_private_networks: tuple[IPNetwork, ...] = field(default_factory=tuple)
    allow_private_hostnames: bool = False

    @classmethod
    def from_cidrs(cls, cidrs: str | list[str] | None, allow_private_hostnames: bool = False) -> NetworkPolicy:
        """Build a policy from a comma-separated string or list of private CIDRs to allow.

        An empty value yields the default policy, which allows only globally routable addresses.
        """
        if isinstance(cidrs, str):
            cidrs = [c.strip() for c in cidrs.split(",") if c.strip()]
        nets = tuple(ipaddress.ip_network(c, strict=False) for c in (cidrs or []))
        return cls(allowed_private_networks=nets, allow_private_hostnames=allow_private_hostnames)

    def ip_block_reason(self, ip: IPAddress) -> str | None:
        """Return why ``ip`` is blocked, or ``None`` if it may be contacted."""
        ip = _unwrap(ip)
        if ip in METADATA_ADDRESSES:
            return "cloud metadata address"
        if ip.is_multicast:
            return "multicast address"
        if ip.is_unspecified:
            return "unspecified address"
        if any(ip in net for net in _EXTRA_BLOCKED):
            return "reserved or non-public address"
        if ip.is_global:
            return None
        if any(ip in net for net in self.allowed_private_networks if net.version == ip.version):
            return None
        if ip.is_loopback:
            return "loopback address"
        if ip.is_link_local:
            return "link-local address"
        if ip.is_private:
            return "private address"
        return "reserved or non-public address"

    def check_ip(self, ip: IPAddress | str) -> None:
        """Raise :class:`BlockedTargetError` if ``ip`` may not be contacted under this policy."""
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
        reason = self.ip_block_reason(addr)
        if reason:
            raise BlockedTargetError(f"destination resolves to a {reason}")


def _unwrap(ip: IPAddress) -> IPAddress:
    """Return the embedded IPv4 address for IPv6 transition formats."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip.teredo:
            return ip.teredo[1]
        if ip in _NAT64 or ip in _SIIT or (ip in _IPV4_COMPATIBLE and int(ip) > 1):
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def parse_ip(host: str) -> IPAddress | None:
    """Return ``host`` as an IP address (brackets allowed for IPv6), or ``None`` if it is a hostname."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def validate_hostname(host: str, policy: NetworkPolicy) -> str:
    """Normalize and syntactically validate a hostname or IP literal.

    Returns the normalized ASCII hostname (IDNA encoded, lowercase, no trailing
    dot) or the compressed IP literal. Raises ``ValueError`` for malformed
    input and ``BlockedTargetError`` for disallowed destinations.
    """
    host = (host or "").strip()
    if not host:
        raise ValueError("hostname is required")
    ip = parse_ip(host)
    if ip is not None:
        policy.check_ip(ip)
        return ip.compressed
    host = host.rstrip(".").lower()
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("hostname is not a valid internationalized domain name") from exc
    if len(ascii_host) > 253:
        raise ValueError("hostname is too long")
    labels = ascii_host.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise ValueError("hostname contains invalid characters")
    if not policy.allow_private_hostnames:
        if ascii_host in BLOCKED_HOSTNAMES or ascii_host.endswith(BLOCKED_SUFFIXES):
            raise BlockedTargetError("hostname refers to a local or internal name")
        if len(labels) < 2:
            raise BlockedTargetError("hostname must be a fully qualified domain name")
        if labels[-1].isdigit():
            raise ValueError("hostname has a numeric top-level domain")
    return ascii_host


def resolve_checked(
    host: str,
    port: int,
    policy: NetworkPolicy,
    timeout_s: float,
    family: int = socket.AF_UNSPEC,
) -> list[tuple[int, IPAddress]]:
    """Resolve ``host`` and validate every address against ``policy``.

    If any resolved address is blocked the whole destination is rejected, so a
    name that mixes public and private answers cannot be used to reach the
    private one. Returns ``(family, ip)`` pairs in resolver order.
    """
    literal = parse_ip(host)
    if literal is not None:
        policy.check_ip(literal)
        fam = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return [(fam, literal)]

    future = _resolver_pool.submit(socket.getaddrinfo, host, port, family, socket.SOCK_STREAM)
    try:
        infos = future.result(timeout=max(timeout_s, 0.001))
    except concurrent.futures.TimeoutError as exc:
        raise ResolutionError("DNS resolution timed out", timeout=True) from exc
    except socket.gaierror as exc:
        nx = exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5))
        raise ResolutionError("DNS resolution failed" + (": name not found" if nx else ""), nxdomain=nx) from exc

    seen: list[tuple[int, IPAddress]] = []
    for fam, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if (fam, ip) not in seen:
            seen.append((fam, ip))
    if not seen:
        raise ResolutionError("DNS resolution returned no addresses")
    for _fam, ip in seen:
        policy.check_ip(ip)
    return seen


def http_port_allowed(port: int) -> bool:
    """HTTP checks may use the standard ports or unprivileged ports.

    Blocking other privileged ports prevents using HTTP checks to talk to
    services such as SMTP (25) or SSH (22) on third-party hosts.
    """
    return port in (80, 443) or 1024 <= port <= 65535

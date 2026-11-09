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

import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field

import dns.exception
import dns.resolver

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

    ``nameservers`` are the resolvers used for resolution-time checks (and DNS checks without a custom
    nameserver). Empty means the host's resolver from ``/etc/resolv.conf``. Public agents should use public
    recursive resolvers so that split-horizon or private zones of the hosting network are never consulted.
    """

    allowed_private_networks: tuple[IPNetwork, ...] = field(default_factory=tuple)
    allow_private_hostnames: bool = False
    nameservers: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_cidrs(
        cls, cidrs: str | list[str] | None, allow_private_hostnames: bool = False, nameservers: str = ""
    ) -> NetworkPolicy:
        """Build a policy from comma-separated (or listed) private CIDRs to allow and nameserver IPs.

        An empty ``cidrs`` yields the default policy, which allows only globally routable addresses.
        """
        if isinstance(cidrs, str):
            cidrs = [c.strip() for c in cidrs.split(",") if c.strip()]
        nets = tuple(ipaddress.ip_network(c, strict=False) for c in (cidrs or []))
        servers = tuple(str(ipaddress.ip_address(s.strip())) for s in nameservers.split(",") if s.strip())
        return cls(allowed_private_networks=nets, allow_private_hostnames=allow_private_hostnames,
                   nameservers=servers)

    def resolver(self) -> dns.resolver.Resolver:
        """Return a dnspython resolver for this policy's nameservers (the system resolver if none)."""
        if not self.nameservers:
            try:
                return dns.resolver.Resolver()
            except dns.resolver.NoResolverConfiguration as exc:
                raise ResolutionError("no DNS resolver configured") from exc
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = list(self.nameservers)
        return resolver

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
    policy: NetworkPolicy,
    timeout_s: float,
    family: int = socket.AF_UNSPEC,
) -> list[tuple[int, IPAddress]]:
    """Resolve ``host`` and validate every address against ``policy``.

    If any resolved address is blocked the whole destination is rejected, so a
    name that mixes public and private answers cannot be used to reach the
    private one. Returns ``(family, ip)`` pairs, IPv4 first.
    """
    literal = parse_ip(host)
    if literal is not None:
        policy.check_ip(literal)
        fam = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return [(fam, literal)]
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        # RFC 6761: localhost names are loopback and are never sent to DNS.
        addrs: list[tuple[int, IPAddress]] = [(socket.AF_INET, ipaddress.ip_address("127.0.0.1")),
                                              (socket.AF_INET6, ipaddress.ip_address("::1"))]
        addrs = [a for a in addrs if family in (socket.AF_UNSPEC, a[0])]
    else:
        addrs = _lookup(name, policy, timeout_s, family)
    for _fam, ip in addrs:
        policy.check_ip(ip)
    return addrs


def _lookup(name: str, policy: NetworkPolicy, timeout_s: float, family: int) -> list[tuple[int, IPAddress]]:
    """Query A and AAAA records for ``name`` within ``timeout_s`` seconds in total.

    dnspython enforces the deadline itself and runs in the calling thread, so an unresponsive nameserver
    can delay only the check that asked; there is no shared resolver pool to exhaust (unlike
    ``getaddrinfo``, which has no timeout). The name is always queried as absolute (``search=False``), so the
    host's resolver search domains (for example Kubernetes ``svc.cluster.local``) are never appended.
    """
    deadline = time.monotonic() + timeout_s
    resolver = policy.resolver()
    queries = [("A", socket.AF_INET), ("AAAA", socket.AF_INET6)]
    out: list[tuple[int, IPAddress]] = []
    for rdtype, fam in queries:
        if family not in (socket.AF_UNSPEC, fam):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResolutionError("DNS resolution timed out", timeout=True)
        resolver.lifetime = resolver.timeout = remaining
        try:
            answer = resolver.resolve(name, rdtype, search=False)
        except dns.resolver.NXDOMAIN as exc:
            raise ResolutionError("DNS resolution failed: name not found", nxdomain=True) from exc
        except dns.resolver.NoAnswer:
            continue
        except dns.resolver.LifetimeTimeout as exc:
            raise ResolutionError("DNS resolution timed out", timeout=True) from exc
        except dns.exception.DNSException as exc:
            raise ResolutionError(f"DNS resolution failed: {type(exc).__name__}") from exc
        for record in answer:
            ip = ipaddress.ip_address(record.address)
            if (fam, ip) not in out:
                out.append((fam, ip))
    if not out:
        raise ResolutionError("DNS resolution returned no addresses")
    return out


def http_port_allowed(port: int) -> bool:
    """HTTP checks may use the standard ports or unprivileged ports.

    Blocking other privileged ports prevents using HTTP checks to talk to
    services such as SMTP (25) or SSH (22) on third-party hosts.
    """
    return port in (80, 443) or 1024 <= port <= 65535

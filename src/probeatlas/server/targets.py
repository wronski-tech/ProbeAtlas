"""Monitor target validation at configuration time.

This is the first, syntactic layer of the outbound network policy. The agent
re-validates resolved addresses at connection time, which is authoritative.
"""

from __future__ import annotations

from typing import Any

from probeatlas.common.netpolicy import BlockedTargetError, NetworkPolicy, validate_hostname
from probeatlas.common.safehttp import validate_http_url
from probeatlas.common.schemas import CheckType

from .errors import APIError


def _invalid(field: str, msg: str) -> APIError:
    """Build a 422 validation error that points at ``field`` in the request body."""
    return APIError(422, "validation_error", "request validation failed",
                    [{"loc": ["body", field], "msg": msg, "type": "value_error"}])


def normalize_target(check_type: CheckType, target: str, settings: dict[str, Any], policy: NetworkPolicy) -> str:
    """Validate and normalize a monitor target for ``check_type``, or raise a 422 :class:`APIError`.

    HTTP targets must be http(s) URLs; DNS, TCP, and ICMP targets must be hostnames or IP literals. Blocked
    destinations (private, loopback, metadata, internal names) are rejected unless the policy allows them.
    """
    try:
        if check_type == CheckType.HTTP:
            return validate_http_url(target, policy)
        if "://" in target or "/" in target:
            raise ValueError(f"{check_type.value} targets must be a hostname or IP address, not a URL")
        host = validate_hostname(target, policy)
        nameserver = settings.get("nameserver")
        if check_type == CheckType.DNS and nameserver:
            try:
                policy.check_ip(nameserver)
            except BlockedTargetError as exc:
                raise _invalid("settings.nameserver", f"nameserver not allowed: {exc}") from exc
        return host
    except BlockedTargetError as exc:
        raise _invalid("target", f"target not allowed: {exc}") from exc
    except ValueError as exc:
        raise _invalid("target", str(exc)) from exc

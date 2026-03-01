from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

logger = logging.getLogger(__name__)


class ApprovalDecision(str, Enum):
    """Decision returned by an approval workflow."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    """Details about a write tool invocation requiring approval."""

    id: str
    tool_name: str
    request_id: str
    client_id: str | None
    arguments: Mapping[str, str]


class ApprovalManager(abc.ABC):
    """Interface for asynchronous approval backends."""

    async def start(self) -> None:
        """Perform any startup/connection work."""

    async def stop(self) -> None:
        """Clean up resources."""

    @abc.abstractmethod
    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        """Require approval for the provided request."""


class NoOpApprovalManager(ApprovalManager):
    """Approval manager that always approves requests.

    When used as a bypass (jesus_take_the_wheel), every write operation is
    auto-approved and audit-logged at WARNING level for forensic visibility.
    """

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        logger.warning(
            "AUTO-APPROVED write operation (no human review): "
            "tool=%s, approval_id=%s, client_id=%s, request_id=%s, arguments=%s",
            request.tool_name,
            request.id,
            request.client_id or "<unknown>",
            request.request_id,
            dict(request.arguments),
        )
        return ApprovalDecision.APPROVED


__all__ = [
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "NoOpApprovalManager",
]

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SupportReply:
    status: str
    route: str
    message: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    ticket_id: str | None = None
    confirmation_token: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


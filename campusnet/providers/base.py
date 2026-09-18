"""Login provider interface.

A provider encapsulates one campus authentication system.  Adding support for
a new system means dropping a module in this package and listing it in
``__init__.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["LoginResult", "Provider"]


@dataclass
class LoginResult:
    ok: bool
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # allows `if provider.login():`
        return self.ok


class Provider:
    """Base class for all providers."""

    #: registry key, e.g. ``ruijie_sam_cas``
    name: str = ""
    #: one-line description
    description: str = ""
    #: option keys that must be present in ``config["options"]``
    required_options: tuple[str, ...] = ()
    #: ready-to-copy options block shown by ``init``
    example_options: dict[str, Any] = {}
    #: extra notes printed by ``init``/``doctor``
    notes: str = ""

    def __init__(self, cfg, client, log) -> None:
        self.cfg = cfg
        self.http = client
        self.log = log

    # -- hooks -------------------------------------------------------------
    def validate(self) -> str | None:
        """Return an error string when the configuration is unusable."""
        if not self.cfg.username:
            return "配置里缺少 username"
        missing = [k for k in self.required_options if not self.cfg.option(k)]
        if missing:
            return "配置 options 里缺少必填项: " + ", ".join(missing)
        return None

    def login(self) -> LoginResult:  # pragma: no cover - abstract
        raise NotImplementedError

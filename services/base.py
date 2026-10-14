"""Shared composition facade for calls between domain service mixins."""

from __future__ import annotations

from threading import RLock
from typing import Any


class _ServiceFacadeMeta(type):
    def __getattr__(cls, name: str) -> Any:
        with cls._lock:
            implementation = cls._implementation
        if implementation is None:
            raise RuntimeError("character service composition is not bound")
        return getattr(implementation, name)


class CharacterService(metaclass=_ServiceFacadeMeta):
    """Resolve cross-domain operations through the composed runtime class."""

    _implementation: type | None = None
    _lock = RLock()

    @classmethod
    def bind(cls, implementation: type) -> None:
        with cls._lock:
            if (
                cls._implementation is not None
                and cls._implementation is not implementation
            ):
                raise RuntimeError(
                    "character service composition is already bound"
                )
            cls._implementation = implementation


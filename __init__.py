"""Production runtime for the reconstructed Drakensang Online server.

Keep package import side-effect free: protocol and state modules also import
foundational modules such as the player database.
"""

from __future__ import annotations

__all__ = ["ServerApplication", "main"]


def __getattr__(name: str):
    if name in __all__:
        from dso_server.app import ServerApplication, main

        return {
            "ServerApplication": ServerApplication,
            "main": main,
        }[name]
    raise AttributeError(name)

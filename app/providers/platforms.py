from __future__ import annotations


from ..config import Settings
from .lobster import LobsterProvider
from .qoder import QoderProvider
from .workbuddy import WorkBuddyProvider


def adapter_for(settings: Settings, http, kind: str):
    if kind in ("workbuddy", "codebuddy"):
        return WorkBuddyProvider(settings, http)
    if kind == "lobster":
        return LobsterProvider(settings, http)
    if kind == "qoder":
        return QoderProvider(settings, http)
    return None



"""Linux client for the Hantek DSO5000P series (DSO5102P / DSO5202P)."""
from .scope import DSO5000P, FullRecord, ScopeError, Waveform
from .settings import Settings, KEYS, LAYOUT

__all__ = ["DSO5000P", "FullRecord", "ScopeError", "Waveform", "Settings", "KEYS", "LAYOUT"]

"""Errors safe to present to a terminal user."""


class MyneError(Exception):
    """An actionable application error."""


class ProviderError(MyneError):
    """A music provider could not complete an operation."""


class PlaybackError(MyneError):
    """Playback or the local player service failed."""

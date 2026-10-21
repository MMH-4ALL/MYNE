"""Errors safe to present to a terminal user."""


class MyneError(Exception):
    """An actionable application error."""


class ProviderError(MyneError):
    """A music provider could not complete an operation."""


class StreamError(ProviderError):
    """A fixed, credential-free explanation safe to forward through the player."""

    MESSAGES = {
        "certificate": "Audio stream TLS verification failed. Check the system certificate store or SSL_CERT_FILE; certificate verification remains enabled.",
        "authentication": "YouTube requires sign-in or account verification for this stream. Configure your own YouTube cookies with MYNE_COOKIES_FILE, then retry.",
        "runtime": "YouTube needs a supported JavaScript runtime. Install Deno 2.3+ or Node.js 22+, then run myne doctor.",
        "denied": "YouTube denied access to the audio stream. Check account or regional restrictions and your configured YouTube cookies.",
        "timeout": "The audio stream request timed out. Check your connection or proxy, then retry.",
        "unavailable": "Could not resolve an audio stream. Update yt-dlp and check your connection or configured YouTube cookies.",
    }

    def __init__(self, reason: str = "unavailable") -> None:
        self.reason = reason if reason in self.MESSAGES else "unavailable"
        super().__init__(self.MESSAGES[self.reason])


class PlaybackError(MyneError):
    """Playback or the local player service failed."""

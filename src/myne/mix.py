"""Mood mixes with an honest offline heuristic and opt-in AI suggestions."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from myne.errors import ProviderError
from myne.models import Track
from myne.plugins import MixStrategy
from myne.providers.base import MusicProvider


class MoodSearchStrategy:
    """Search suggestions, explicitly not an AI model."""

    _moods = {
        "gym": ["workout energetic rock", "workout hip hop", "high energy electronic"],
        "focus": ["instrumental focus", "ambient study", "lofi concentration"],
        "night drive": ["night drive synthwave", "late night electronic", "night drive indie"],
    }

    def suggest(self, mood: str, limit: int) -> list[str]:
        return self._moods.get(mood.casefold(), [f"{mood} music", f"{mood} songs"])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Never forward an API Authorization header to a redirect destination.
        return None


class AIMixStrategy:
    """Use a configured OpenAI-compatible chat endpoint to suggest real songs.

    The endpoint is the complete chat-completions URL. Suggestions are metadata
    only; MixService resolves each query with the active music provider.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        model: str = "myne-mix",
        timeout: float = 20,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            valid = (
                (
                    parsed.scheme == "https"
                    or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
                )
                and bool(parsed.hostname)
                and not (parsed.username or parsed.password or parsed.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("The AI mix endpoint must be an HTTPS URL (or local HTTP server).")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ProviderError("Configure a valid AI mix model name.")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ProviderError("The AI mix timeout must be between 0 and 120 seconds.")
        if api_key is not None and (not isinstance(api_key, str) or any(c in api_key for c in "\r\n")):
            raise ProviderError("Configure a valid AI mix API key.")
        self.endpoint, self.api_key, self.model, self.timeout = endpoint, api_key, model, timeout
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open

    @classmethod
    def from_environment(cls) -> AIMixStrategy | None:
        endpoint = os.environ.get("MYNE_MIX_ENDPOINT")
        if not endpoint:
            return None
        return cls(
            endpoint,
            api_key=os.environ.get("MYNE_MIX_API_KEY"),
            model=os.environ.get("MYNE_MIX_MODEL", "myne-mix"),
        )

    def suggest(self, mood: str, limit: int) -> list[str]:
        _validate_request(mood, limit)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You curate music playlists. Return only a JSON object with a 'queries' array "
                        "of distinct existing songs formatted as 'artist - song title'. "
                        "Do not invent tracks, include URLs, or include commentary."
                    ),
                },
                {"role": "user", "content": f"Suggest {limit} songs for this mood: {mood}"},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("Response too large")
            envelope = json.loads(raw)
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str) or len(content) > 100_000:
                raise ValueError("Invalid content")
            suggestions = json.loads(content)
            queries = suggestions.get("queries") if isinstance(suggestions, Mapping) else None
            if not isinstance(queries, list) or not queries or len(queries) > 100:
                raise ValueError("Invalid query list")
            if any(
                not isinstance(query, str)
                or not query.strip()
                or len(query) > 300
                or any(ord(c) < 32 for c in query)
                or "://" in query
                for query in queries
            ):
                raise ValueError("Invalid query")
            return list(dict.fromkeys(query.strip() for query in queries))[:limit]
        except HTTPError as error:
            if error.code in (401, 403):
                message = "AI mix authentication failed. Check MYNE_MIX_API_KEY."
            elif error.code == 429:
                message = "The AI mix service is rate limited. Try again later."
            else:
                message = "The AI mix service rejected the request. Check the endpoint and model."
            raise ProviderError(message) from None
        except (URLError, OSError, TimeoutError):
            raise ProviderError("The AI mix service is unreachable or timed out.") from None
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            raise ProviderError("The AI mix service returned invalid song suggestions.") from None


def _validate_request(mood: str, limit: int) -> None:
    if not isinstance(mood, str) or not mood.strip() or len(mood) > 300:
        raise ProviderError("Enter a mood between 1 and 300 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ProviderError("The mix size must be between 1 and 100 songs.")


class MixService:
    def __init__(self, provider: MusicProvider, strategy: MixStrategy | None = None) -> None:
        self.provider = provider
        self.strategy = strategy if strategy is not None else MoodSearchStrategy()

    def generate(self, mood: str, limit: int = 20) -> list[Track]:
        _validate_request(mood, limit)
        try:
            queries = self.strategy.suggest(mood.strip(), limit)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("The selected mix strategy failed.") from None
        if (
            not isinstance(queries, list)
            or not queries
            or any(not isinstance(query, str) or not query.strip() or len(query) > 1000 for query in queries)
        ):
            raise ProviderError("The mix strategy returned invalid search suggestions.")
        tracks: list[Track] = []
        seen: set[tuple[str, str]] = set()
        # Heuristic queries describe a category; AI/plugin suggestions name songs.
        per_query = (
            max(1, (limit + len(queries) - 1) // len(queries))
            if isinstance(self.strategy, MoodSearchStrategy)
            else 1
        )
        for query in queries[:100]:
            for track in self.provider.search(query.strip(), limit=per_query):
                identity = (track.provider, track.id)
                if identity not in seen:
                    seen.add(identity)
                    tracks.append(track)
                    if len(tracks) == limit:
                        return tracks
        if not tracks:
            raise ProviderError("No playable songs matched this mood. Try another mood or music provider.")
        return tracks

"""Explicitly enabled entry-point plugins and injectable provider factories.

A plugin is installed Python code with the user's permissions. Discovery never
imports plugins; only names in the enabled configuration are loaded.
"""

import re
from collections.abc import Callable
from importlib.metadata import entry_points
from threading import RLock
from typing import Protocol

from myne.errors import ProviderError
from myne.providers.base import MusicProvider
from myne.providers.local import LocalMusicProvider
from myne.providers.youtube import YouTubeMusicProvider


class MixStrategy(Protocol):
    def suggest(self, mood: str, limit: int) -> list[str]:
        """Return provider search queries, normally 'artist - song' names."""
        ...


ProviderFactory = Callable[[], MusicProvider]


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise ProviderError("Extension names must use lowercase letters, digits, '-' or '_'.")


class PluginRegistry:
    """Composition root extension registry; failed registrations roll back."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderFactory] = {
            "youtube": YouTubeMusicProvider,
            "local": LocalMusicProvider,
        }
        self._mixes: dict[str, MixStrategy] = {}
        self._instances: dict[str, MusicProvider] = {}
        self._lock = RLock()
        self._loaded: set[str] = set()
        self.diagnostics: list[str] = []

    @property
    def provider_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._providers))

    @property
    def mix_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._mixes))

    def register_provider(self, name: str, factory: ProviderFactory) -> None:
        """Register or override a factory; instances are created on demand."""
        _validate_name(name)
        if not callable(factory):
            raise ProviderError("A provider factory must be callable.")
        with self._lock:
            self._providers[name] = factory
            self._instances.pop(name, None)

    def provider(self, name: str) -> MusicProvider:
        with self._lock:
            if name in self._instances:
                return self._instances[name]
            if name not in self._providers:
                raise ProviderError(f"Unknown music provider: {name}.")
            try:
                provider = self._providers[name]()
                if not isinstance(provider, MusicProvider) or provider.name != name:
                    raise TypeError("Provider does not satisfy the extension contract")
                self._instances[name] = provider
                return provider
            except ProviderError:
                raise
            except Exception:
                raise ProviderError(f"Music provider '{name}' could not be initialized.") from None

    def register_mix(self, name: str, strategy: MixStrategy) -> None:
        _validate_name(name)
        if not callable(getattr(strategy, "suggest", None)):
            raise ProviderError("A mix strategy must provide suggest(mood, limit).")
        with self._lock:
            self._mixes[name] = strategy

    def mix(self, name: str) -> MixStrategy:
        with self._lock:
            try:
                return self._mixes[name]
            except KeyError:
                raise ProviderError(f"Unknown mix strategy: {name}.") from None

    def load_plugins(self, enabled: tuple[str, ...]) -> list[str]:
        """Load ``myne.plugins`` entry points only when explicitly enabled.

        A plugin entry point exports ``register(registry)``. Repeated loads are
        idempotent. A broken plugin leaves the previous registries unchanged and
        cannot prevent other explicitly enabled plugins from loading.
        """
        with self._lock:
            return self._load_plugins(enabled)

    def _load_plugins(self, enabled: tuple[str, ...]) -> list[str]:
        diagnostics: list[str] = []
        try:
            discovered = entry_points(group="myne.plugins")
        except Exception:
            diagnostics.append("Could not discover installed Myne plugins.")
            self.diagnostics.extend(diagnostics)
            return diagnostics
        for name in dict.fromkeys(enabled):
            if name in self._loaded:
                continue
            candidates = [entry for entry in discovered if entry.name == name]
            if len(candidates) != 1:
                reason = "not installed" if not candidates else "registered more than once"
                diagnostics.append(f"Plugin '{name}' is {reason}; it was not loaded.")
                continue
            providers, mixes, instances = self._providers.copy(), self._mixes.copy(), self._instances.copy()
            try:
                register = candidates[0].load()
                if not callable(register):
                    raise TypeError("Plugin must export a registration function")
                register(self)
            except Exception:
                self._providers, self._mixes, self._instances = providers, mixes, instances
                diagnostics.append(f"Plugin '{name}' failed to load; its registrations were discarded.")
            else:
                self._loaded.add(name)
        self.diagnostics.extend(diagnostics)
        return diagnostics

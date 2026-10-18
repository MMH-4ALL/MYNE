"""The composition root: infrastructure is injected into application services."""

from myne.config import Config
from myne.plugins import PluginRegistry
from myne.providers.local import LocalMusicProvider
from myne.providers.youtube import YouTubeMusicProvider


def create_registry(config: Config) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_provider("youtube", lambda: YouTubeMusicProvider(config.auth_file, config.cookies_file))
    registry.register_provider("local", lambda: LocalMusicProvider(config.library_path))
    registry.load_plugins(config.enabled_plugins)
    return registry

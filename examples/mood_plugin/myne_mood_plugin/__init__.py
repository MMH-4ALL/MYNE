"""Minimal metadata-only plugin; no audio data is downloaded."""

from myne.plugins import PluginRegistry


class NightDriveMix:
    def suggest(self, mood: str, limit: int) -> list[str]:
        return [
            "Kavinsky - Nightcall",
            "M83 - Midnight City",
            "The Midnight - Sunset",
            "College - A Real Hero",
        ][:limit]


def register(registry: PluginRegistry) -> None:
    registry.register_mix("night-drive", NightDriveMix())

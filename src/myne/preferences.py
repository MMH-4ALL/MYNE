"""Validated, backwards-compatible user preferences."""

from dataclasses import asdict, dataclass
from typing import Any

from myne.errors import MyneError

CHOICES = {
    "theme": ("myne-studio", "myne-midnight", "myne-ember"),
    "sidebar": ("auto", "show", "hide"),
    "lyrics": ("auto", "show", "hide"),
}


@dataclass(frozen=True, slots=True)
class Preferences:
    theme: str = "myne-studio"
    sidebar: str = "auto"
    lyrics: str = "auto"
    visualizer: bool = True
    reduced_motion: bool = False
    auto_check_updates: bool = False
    volume: int = 70

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Any, *, strict: bool = False) -> "Preferences":
        if not isinstance(values, dict):
            if strict:
                raise MyneError("Settings must be an object of named values.")
            return cls()
        defaults = asdict(cls())
        clean: dict[str, Any] = {}
        if strict and values.keys() - defaults.keys():
            raise MyneError("Unknown setting. Open Settings to see the supported options.")
        for key, default in defaults.items():
            value = values.get(key, default)
            valid = (isinstance(value, str) and value in CHOICES[key]) if key in CHOICES else (
                type(value) is int and 0 <= value <= 100 if key == "volume" else type(value) is bool
            )
            if not valid and strict:
                hint = ", ".join(CHOICES[key]) if key in CHOICES else "an integer from 0 to 100" if key == "volume" else "true or false"
                raise MyneError(f"{key.replace('_', ' ').title()} must be {hint}.")
            clean[key] = value if valid else default
        return cls(**clean)

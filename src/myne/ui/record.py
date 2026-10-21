"""A terminal-native vinyl motif, separate from provider album artwork."""

from __future__ import annotations

import math

from rich.color import Color
from rich.text import Text
from textual.widget import Widget


class RecordArt(Widget):
    """A static record sleeve that adapts to the available terminal cells.

    This is MYNE's own graphic, never a fetched album cover or audio measurement.
    It stays still, including during playback, and performs no network requests.
    """

    DEFAULT_CSS = """
    RecordArt { height: 1fr; min-height: 7; content-align: center middle; }
    """

    def render(self) -> Text:
        accent = self.app.current_theme.primary
        color = Color.parse(accent).get_truecolor()
        shade = f"rgb({color.red // 3},{color.green // 3},{color.blue // 3})"
        rows = max(3, min(17, self.content_size.height))
        if rows % 2 == 0:
            rows -= 1
        columns = min(rows * 2 + 2, max(8, self.content_size.width - 4))
        result = Text(no_wrap=True)
        for y in range(rows):
            for x in range(columns):
                nx = (x - (columns - 1) / 2) / (columns * 0.46)
                ny = (y - (rows - 1) / 2) / (rows * 0.47)
                radius = math.hypot(nx, ny)
                if radius < 0.10:
                    char, style = "·", f"{accent} on {shade}"
                elif radius < 0.31:
                    char, style = "█", accent
                elif radius < 0.98:
                    char = "░" if int(radius * 26) % 3 == 0 else "▒"
                    style = f"{shade} on {self.app.current_theme.background}"
                elif radius < 1.06:
                    char, style = "▓", shade
                else:
                    char, style = " ", ""
                result.append(char, style=style)
            if y != rows - 1:
                result.append("\n")
        return result

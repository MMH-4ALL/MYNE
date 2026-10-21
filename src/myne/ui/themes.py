"""Three coherent, dark terminal palettes. The terminal owns the font."""

from textual.theme import Theme

THEMES = (
    Theme(name="myne-studio", primary="#b7ed7e", secondary="#b7ed7e", accent="#b7ed7e",
          background="#101516", surface="#171e20", panel="#222c2e", foreground="#e9efea",
          error="#ff9b92", warning="#edc17e"),
    Theme(name="myne-midnight", primary="#85c8f2", secondary="#85c8f2", accent="#85c8f2",
          background="#101724", surface="#182235", panel="#253249", foreground="#e7edf7",
          error="#ff9b92", warning="#edc17e"),
    Theme(name="myne-ember", primary="#f2b17e", secondary="#f2b17e", accent="#f2b17e",
          background="#191615", surface="#241e1b", panel="#352b25", foreground="#f0e8e0",
          error="#ff9b92", warning="#edc17e"),
)
THEME_NAMES = tuple(theme.name for theme in THEMES)

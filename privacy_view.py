"""Display-only masking. Broker inputs, quote levels and journals keep their original values."""
import re
import tkinter as tk

_MONEY = re.compile(r"[-+]?(?:\$|USD\s+)\s*[+-]?\d[\d,]*(?:\.\d+)?|[-+]?\d[\d,]*(?:\.\d+)?\s*(?:USD|美元)(?!/份)")


def mask_money(text):
    return _MONEY.sub("•••", str(text))


class BeanStrip(tk.Canvas):
    """Tile the card viewport independently of its balance and requested size."""

    def __init__(self, parent, icons):
        super().__init__(parent, width=1, height=1, highlightthickness=0,
                         borderwidth=0, background="#fff1c7", takefocus=False)
        self.icons = icons
        self.bind("<Configure>", self.draw)

    def draw(self, event):
        self.delete("all")
        width, height = event.width, event.height
        faces = next((pair for pair in self.icons if pair[0].height() <= height), self.icons[-1])
        columns = max(1, width // faces[0].width())
        rows = max(1, height // faces[0].height())
        for row in range(rows):
            for col in range(columns):
                self.create_image((col + 0.5) * width / columns, (row + 0.5) * height / rows,
                                  image=faces[(row + col) % 2])

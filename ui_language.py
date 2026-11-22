"""Offline display language. Broker values and form model keys stay canonical."""
from functools import lru_cache
import json
from pathlib import Path
import re
import weakref

_HAN = re.compile(r"[\u3400-\u9fff]")
_PUNCT = str.maketrans({"：": ": ", "，": ", ", "；": "; ", "。": ".", "（": " (", "）": ")", "、": ", "})


@lru_cache(maxsize=1)
def _catalog():
    phrases = json.loads(Path(__file__).with_name("ui_en.json").read_text(encoding="utf-8"))
    # Also match literal fragments of composed status lines, retaining quote values,
    # symbols, IDs and anything the broker supplied between those fragments.
    pattern = re.compile("|".join(re.escape(key) for key in sorted(phrases, key=len, reverse=True)))
    return phrases, pattern


@lru_cache(maxsize=4096)
def english(value):
    if not _HAN.search(value):
        return value
    phrases, pattern = _catalog()
    if value in phrases:
        return phrases[value]
    def replace(match):
        start, end = match.span()
        translated = phrases[match.group()]
        if start and value[start-1].isalnum() and translated[:1].isalnum():
            translated = " " + translated
        if end < len(value) and not value[end].isspace():
            needs_word_space = (translated[-1:].isalnum() and value[end].isascii()
                                and value[end].isalnum() and _HAN.search(value[end-1]))
            if translated.endswith(":") or needs_word_space:
                translated += " "
        return translated
    return pattern.sub(replace, value).translate(_PUNCT)


class Language:
    def __init__(self, preference):
        self.preference = preference
        try:
            self.code = "en" if preference.read_text(encoding="ascii").strip() == "en" else "zh"
        except (OSError, UnicodeError):
            self.code = "zh"
        self.widgets = weakref.WeakSet()

    def toggle(self):
        self.code = "en" if self.code == "zh" else "zh"
        for widget in tuple(self.widgets):
            if widget.winfo_exists():
                widget.refresh_language()
            else:
                self.widgets.discard(widget)
        try:
            self.preference.parent.mkdir(parents=True, exist_ok=True)
            self.preference.write_text(self.code, encoding="ascii")
        except OSError:
            return False
        return True

    def text(self, value):
        return english(value) if self.code == "en" and isinstance(value, str) else value


def language_of(widget):
    root = getattr(widget, "_root", lambda: None)()
    language = getattr(root, "ui_language", None)
    return language if isinstance(language, Language) else None


def display(widget, text):
    language = language_of(widget)
    return language.text(text) if language else text


def window_title(window, title):
    window.source_title = title
    window.title(display(window, title))

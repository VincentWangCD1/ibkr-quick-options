"""Tk presentation adapters. Widgets keep their source text across language switches."""
import tkinter as tk
from tkinter import ttk
from tkinter.ttk import Entry, Frame, Scale, Scrollbar, Separator, Style

from ui_language import display, language_of


def _watch(widget):
    language = language_of(widget)
    if language:
        language.widgets.add(widget)


class _Caption:
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._source_text = kw.get("text")
        self._source_var = kw.get("textvariable")
        self._source_trace = None
        if self._source_var is not None:
            self._translated = tk.StringVar(master=self)
            super().configure(textvariable=self._translated)
            self._source_trace = self._source_var.trace_add("write", self._sync_text)
        _watch(self)
        self.refresh_language()

    def _sync_text(self, *_):
        value = display(self, self._source_var.get())
        if self._translated.get() != value:
            self._translated.set(value)

    def refresh_language(self):
        if self._source_var is not None:
            self._sync_text()
        elif self._source_text is not None:
            super().configure(text=display(self, self._source_text))

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if "text" in kw:
            self._source_text = kw["text"]
            kw["text"] = display(self, kw["text"])
        return super().configure(cnf, **kw)

    config = configure

    def destroy(self):
        if self._source_trace:
            self._source_var.trace_remove("write", self._source_trace)
        super().destroy()


class Label(_Caption, ttk.Label):
    pass


class Button(_Caption, ttk.Button):
    pass


class Checkbutton(_Caption, ttk.Checkbutton):
    pass


class Menubutton(_Caption, ttk.Menubutton):
    pass


class LabelFrame(_Caption, ttk.LabelFrame):
    pass


class ClassicButton(_Caption, tk.Button):
    pass


class ClassicLabel(_Caption, tk.Label):
    pass


class Combobox(ttk.Combobox):
    """Bind the standard Tk editor to a display variable, not to translated order keys."""
    def __init__(self, master=None, **kw):
        self._model = kw.pop("textvariable", None)
        if self._model is None:
            self._model = tk.StringVar(master=master)
        self._choices = tuple(kw.get("values", ()))
        self._editor = tk.StringVar(master=master)
        self._syncing = False
        super().__init__(master, textvariable=self._editor, **kw)
        self._model_trace = self._model.trace_add("write", self._from_model)
        self._editor_trace = self._editor.trace_add("write", self._from_editor)
        _watch(self)
        self.refresh_language()

    def _from_model(self, *_):
        value = str(self._model.get())
        shown = self._labels.get(value, value)
        if self._editor.get() != shown:
            self._syncing = True
            try:
                self._editor.set(shown)
            finally:
                self._syncing = False

    def _from_editor(self, *_):
        if not self._syncing:
            edited = self._editor.get()
            key = self._keys.get(edited, edited)
            if str(self._model.get()) != key:
                self._model.set(key)

    def refresh_language(self):
        self._labels = {str(key): display(self, str(key)) for key in self._choices}
        self._keys = {label: key for key, label in self._labels.items()}
        super().configure(values=tuple(self._labels.values()))
        self._from_model()

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if "values" in kw:
            self._choices = tuple(kw.pop("values"))
            self.refresh_language()
            if not kw:
                return
        return super().configure(cnf, **kw)

    config = configure

    def destroy(self):
        self._model.trace_remove("write", self._model_trace)
        self._editor.trace_remove("write", self._editor_trace)
        super().destroy()


class Notebook(ttk.Notebook):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._captions = {}
        _watch(self)

    def add(self, child, **kw):
        if "text" in kw:
            self._captions[str(child)] = kw["text"]
            kw["text"] = display(self, kw["text"])
        return super().add(child, **kw)

    def insert(self, pos, child, **kw):
        if "text" in kw:
            self._captions[str(child)] = kw["text"]
            kw["text"] = display(self, kw["text"])
        return super().insert(pos, child, **kw)

    def refresh_language(self):
        for child, label in self._captions.items():
            self.tab(child, text=display(self, label))


class Treeview(ttk.Treeview):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._headings = {}
        self._rows = {}
        _watch(self)

    def heading(self, column, option=None, **kw):
        if "text" in kw:
            self._headings[column] = kw["text"]
            kw["text"] = display(self, kw["text"])
        return super().heading(column, option, **kw)

    def _values(self, values):
        return tuple(display(self, val) for val in values)

    def insert(self, parent, index, iid=None, **kw):
        values = kw.get("values")
        if values is not None:
            kw["values"] = self._values(values)
        row = super().insert(parent, index, iid, **kw)
        if values is not None:
            self._rows[row] = tuple(values)
        return row

    def item(self, item, option=None, **kw):
        if "values" in kw:
            self._rows[str(item)] = tuple(kw["values"])
            kw["values"] = self._values(kw["values"])
        return super().item(item, option, **kw)

    def delete(self, *items):
        super().delete(*items)
        for item in items:
            self._rows.pop(str(item), None)

    def refresh_language(self):
        for col, label in self._headings.items():
            super().heading(col, text=display(self, label))
        for row, values in self._rows.items():
            super().item(row, values=self._values(values))


class Menu(tk.Menu):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._labels = {}
        _watch(self)

    def add_command(self, cnf=None, **kw):
        options = {**(cnf or {}), **kw}
        label = options.get("label", "")
        options["label"] = display(self, label)
        super().add_command(**options)
        self._labels[self.index("end")] = label

    def refresh_language(self):
        for index, label in self._labels.items():
            self.entryconfigure(index, label=display(self, label))


class Toplevel(tk.Toplevel):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        _watch(self)

    def title(self, string=None):
        if string is None:
            return super().title()
        self._source_title = string
        return super().title(display(self, string))

    def refresh_language(self):
        if hasattr(self, "_source_title"):
            self.title(self._source_title)


class TextCanvas(tk.Canvas):
    def create_text(self, *coords, **kw):
        if "text" in kw:
            kw["text"] = display(self, kw["text"])
        return super().create_text(*coords, **kw)

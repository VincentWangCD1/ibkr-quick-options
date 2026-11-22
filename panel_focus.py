"""Expand the existing notebook in place, preserving widgets and broker state."""
import tkinter as tk
import localized_widgets as ttk


class PanelFocusMixin:
    def build_work_area(self, outer):
        self.workspace_outer = outer
        self.panel_focus_active = False
        self.focus_saved = None
        self.workspace = ttk.Frame(outer)
        self.workspace.grid(row=5, column=0, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self.focus_title = self.var("工作区")
        ttk.Style(self.root).configure("Compact.TButton", font=("Microsoft YaHei UI", 8), padding=2)
        self.focus_button = ttk.Button(self.workspace, text="⛶ 最大化查看当前页", style="Compact.TButton", command=self.toggle_panel_focus)
        self.focus_pin = ttk.Checkbutton(self.workspace, text="窗口置顶", variable=self.pin_window, command=self.apply_window_pin)
        self.focus_language = ttk.Button(self.workspace, textvariable=self.language_action,
                                         style="Compact.TButton", command=self.toggle_language)
        self.focus_button.place(relx=1, x=-2, y=0, anchor="ne")
        self.root.bind("<Escape>", self.focus_escape, add="+")

    def update_focus_title(self):
        if hasattr(self, "focus_title") and self.notebook.select():
            title = self.notebook.tab(self.notebook.select(), "text")
            self.focus_title.set(title + (" · 最大化查看（Esc 或 × 返回）" if self.panel_focus_active else ""))

    def toggle_panel_focus(self):
        if self.panel_focus_active:
            self.restore_panel_focus()
            return
        hidden = []
        for widget in self.workspace_outer.grid_slaves():
            if widget is not self.workspace and int(widget.grid_info().get("row", 5)) < 5:
                hidden.append(widget)
                widget.grid_remove()
        self.focus_saved = dict(state=self.root.state(), geometry=self.root.geometry(), hidden=hidden)
        self.panel_focus_active = True
        self.focus_button.configure(text="× 返回默认布局")
        self.focus_pin.place(in_=self.focus_button, relx=0, x=-5, y=1, anchor="ne")
        self.focus_pin.lift()
        self.focus_language.place(in_=self.focus_pin, relx=0, x=-5, y=0, anchor="ne")
        self.focus_language.lift()
        self.root.protocol("WM_DELETE_WINDOW", self.restore_panel_focus)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass  # In-place expansion still works on platforms without zoomed.
        self.update_focus_title()

    def restore_panel_focus(self):
        if not getattr(self, "panel_focus_active", False):
            return
        saved = self.focus_saved
        self.panel_focus_active = False
        for widget in saved["hidden"]:
            widget.grid()
        if hasattr(self, "connection_panel"):
            self.show_connection_details(self.connection_expanded)
        self.root.state(saved["state"])
        if saved["state"] == "normal":
            self.root.geometry(saved["geometry"])
        self.focus_saved = None
        self.focus_pin.place_forget()
        self.focus_language.place_forget()
        self.focus_button.configure(text="⛶ 最大化查看当前页")
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.update_focus_title()

    def focus_escape(self, event):
        if getattr(self, "panel_focus_active", False) and event.widget.winfo_toplevel() == self.root:
            self.restore_panel_focus()
            return "break"

    def show_connection_details(self, expanded):
        self.connection_expanded = bool(expanded)
        self.connection_toggle.configure(text="收起账户设置" if expanded else "账户设置 / 断开")
        if not getattr(self, "panel_focus_active", False):
            if expanded:
                self.connection_panel.grid()
            else:
                self.connection_panel.grid_remove()

    def toggle_entry_options(self, expanded=None):
        self.entry_options_expanded = not self.entry_options_expanded if expanded is None else expanded
        for widget in self.entry_option_widgets:
            widget.grid() if self.entry_options_expanded else widget.grid_remove()
        self.entry_options_button.configure(text="收起高级参数 ▴" if self.entry_options_expanded else "高级参数 / 指定合约 ▾")

    def toggle_chain_reference(self):
        expanded = not bool(self.chain_reference_bar.winfo_manager())
        self.chain_reference_bar.grid() if expanded else self.chain_reference_bar.grid_remove()
        self.chain_reference_button.configure(text="收起说明 ▴" if expanded else "参考/说明 ▾")

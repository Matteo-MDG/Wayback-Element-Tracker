import sys
import os
import re
import threading
import requests
from wayback_element_tracker import DEFAULT_SETTINGS, VERSION, GITHUB_REPO

# -- GUI -----------------------------------------------------------------------
import tkinter as tk
import tkinter.font as _tkfont
from tkinter import ttk, scrolledtext, messagebox, filedialog
import queue as _queue
import io as _io
import subprocess
import ctypes as _ctypes
import webbrowser

# Enable DPI awareness before any Tk window is created so controls render
# sharply on high-DPI displays.  Requires Windows 8.1+ (shcore).
if sys.platform == "win32":
    try:
        _ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

SETTINGS_PATH = "settings.txt"
YES_NO = ["yes", "no"]

# -- Keyboard shortcut helpers -------------------------------------------------
_SC_MOD_MAP = {
    "ctrl": "Control", "control": "Control",
    "alt": "Alt", "shift": "Shift",
    "win": "Win", "cmd": "Command", "command": "Command", "meta": "Meta",
}
_SC_KEY_MAP = {
    "tab": "Tab", "escape": "Escape", "esc": "Escape",
    "enter": "Return", "return": "Return",
    "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete",
    "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "insert": "Insert",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}

def _shortcut_to_tk(s: str) -> str:
    """Convert 'Ctrl+S' / 'Alt+1' style string to a Tkinter event sequence."""
    s = s.strip()
    if not s:
        return ""
    parts = [p.strip() for p in s.split("+") if p.strip()]
    if not parts:
        return ""
    mods = []
    key_part = None
    for p in parts:
        m = _SC_MOD_MAP.get(p.lower())
        if m:
            if m not in mods:
                mods.append(m)
        else:
            key_part = p
    if key_part is None:
        return ""
    kl = key_part.lower()
    if kl in _SC_KEY_MAP:
        tk_key = _SC_KEY_MAP[kl]
    elif len(key_part) == 1:
        c = key_part
        # Alt+digit needs the "Key-" prefix in Tkinter on Windows/Linux
        if c.isdigit() and "Alt" in mods:
            tk_key = f"Key-{c}"
        else:
            tk_key = c.lower()
    else:
        tk_key = key_part  # pass-through for unknown names
    seq = ("-".join(mods) + "-" + tk_key) if mods else tk_key
    return f"<{seq}>"

def _capture_shortcut(root, var, btn):
    """Temporarily capture the next keypress and write it to *var* as 'Mod+Key'.
    Clicking the button again (shown as 'Cancel') cancels without changing the value."""
    _handler = [None]

    def _cancel():
        root.unbind("<KeyPress>", _handler[0])
        btn.configure(text="Capture", command=lambda: _capture_shortcut(root, var, btn))

    btn.configure(text="Cancel", command=_cancel)

    def _on_key(e):
        keysym = e.keysym
        # Ignore standalone modifier keypresses - keep waiting
        if keysym in ("Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L",
                      "Shift_R", "Win_L", "Win_R", "Super_L", "Super_R",
                      "Meta_L", "Meta_R"):
            return
        root.unbind("<KeyPress>", _handler[0])
        btn.configure(text="Capture", command=lambda: _capture_shortcut(root, var, btn))
        parts = []
        state = e.state
        if state & 0x4:                       parts.append("Ctrl")
        if state & 0x8 or state & 0x20000:    parts.append("Alt")
        if state & 0x1:                       parts.append("Shift")
        # Friendly key name
        friendly = {
            "Tab": "Tab", "Escape": "Escape", "Return": "Enter",
            "space": "Space", "BackSpace": "Backspace", "Delete": "Delete",
            "Prior": "PageUp", "Next": "PageDown",
            **{f"F{n}": f"F{n}" for n in range(1, 13)},
        }
        kl = keysym.lower()
        if kl in _SC_KEY_MAP:
            parts.append(friendly.get(_SC_KEY_MAP[kl], _SC_KEY_MAP[kl]))
        elif len(keysym) == 1:
            parts.append(keysym.upper())
        else:
            parts.append(keysym)
        var.set("+".join(parts))
        return "break"

    _handler[0] = root.bind("<KeyPress>", _on_key, add="+")

# -- Tooltip texts ------------------------------------------------------------
from wayback_dialogs import _TIPS, _DIALOGS, _ERRORS


def _read_raw_settings(path=SETTINGS_PATH):
    """Parse settings.txt into a flat dict, seeded from DEFAULT_SETTINGS."""
    raw = {}
    for line in DEFAULT_SETTINGS.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        raw[k.strip().lower()] = v.strip()
    # Seed extract defaults for element_N keys present in DEFAULT_SETTINGS
    for _k in list(raw):
        if re.match(r'^element_\d+$', _k):
            _n = _k[len("element_"):]
            raw.setdefault(f"extract_{_n}", "text")

    if not os.path.exists(path):
        return raw

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip().lower()
            # Accept known settings keys and any dynamic element_N / extract_N keys.
            if k in raw or re.match(r'^(element|extract)_\d+$', k):
                raw[k] = v.strip()

    # Seed extract defaults for any element_N loaded from the file.
    for _k in list(raw):
        if re.match(r'^element_\d+$', _k):
            _n = _k[len("element_"):]
            raw.setdefault(f"extract_{_n}", "text")

    return raw


def _write_settings(raw: dict, path=SETTINGS_PATH):
    """Re-write settings.txt from *raw*, preserving comments and section headers.

    Element slots are written dynamically (element_1 … element_N in slot order)
    rather than mirroring the fixed template in DEFAULT_SETTINGS.
    """
    lines = []
    _element_block_written = False

    for line in DEFAULT_SETTINGS.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        k, _, _ = stripped.partition("=")
        k = k.strip().lower()

        if re.match(r'^(element|extract)_\d+$', k):
            # On the first element/extract template line, emit all active slots.
            if not _element_block_written:
                _element_block_written = True
                slot_nums = sorted(
                    int(m.group(1))
                    for ek in raw
                    if (m := re.match(r'^element_(\d+)$', ek))
                    and raw.get(ek, "").strip()
                )
                # Only write up to the last non-empty element so that removed
                # slots (which are cleared to "" in raw) are not persisted.
                last_nonempty = max(
                    (n for n in slot_nums if raw.get(f"element_{n}")),
                    default=0,
                )
                for n in slot_nums:
                    if n > last_nonempty:
                        break
                    lines.append(f"element_{n} = {raw.get(f'element_{n}', '')}")
                    lines.append(f"extract_{n} = {raw.get(f'extract_{n}', 'text')}")
                    if n < last_nonempty:
                        lines.append("")
            # Skip the DEFAULT_SETTINGS template element/extract lines.
            continue

        lines.append(f"{k} = {raw.get(k, '')}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class _Tooltip:
    """Lightweight hover tooltip attached to any widget."""
    def __init__(self, widget, text, follow_cursor=False):
        self._widget = widget
        self._text = text
        self._follow_cursor = follow_cursor
        self._win = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event=None):
        if self._win or not self._text:
            return
        self._win = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(background="#b0b0b0")   # 1 px border colour
        _fnt = _tkfont.Font(family="Segoe UI", size=9)
        max_line_px = max((_fnt.measure(line) for line in self._text.split("\n")), default=0)
        lbl = tk.Label(tw, text=self._text, justify="left",
                       background="#ffffff", foreground="#000000",
                       relief="flat", borderwidth=0,
                       font=("Segoe UI", 9),
                       wraplength=max_line_px + 1, padx=7, pady=4)
        lbl.pack(padx=1, pady=1)
        tw.update_idletasks()
        # Use the label's requested size (reliable on unmapped overrideredirect windows,
        # unlike tw.winfo_width() which returns 1 until the window is actually rendered).
        tw_w = lbl.winfo_reqwidth() + 2   # +2 for pack(padx=1) on each side
        tw_h = lbl.winfo_reqheight() + 2  # +2 for pack(pady=1) on each side
        sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
        if self._follow_cursor and event is not None:
            # Position just below and to the right of the cursor
            x = event.x_root + 16
            y = event.y_root + 16
            if x + tw_w > sw:
                x = event.x_root - tw_w - 16
        else:
            # Position to the right of the widget; flip left if it doesn't fit
            x = self._widget.winfo_rootx() + self._widget.winfo_width() + 6
            y = self._widget.winfo_rooty()
            if x + tw_w > sw:
                x = self._widget.winfo_rootx() - tw_w - 6
        if y + tw_h > sh:
            y = sh - tw_h
        # Clamp to screen bounds so the tooltip is never partially off-screen
        x = max(0, min(x, sw - tw_w))
        y = max(0, min(y, sh - tw_h))
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self, event=None):
        if self._win:
            self._win.destroy()
            self._win = None

    def update(self, text):
        """Change the tooltip text; refreshes immediately if currently shown."""
        self._text = text
        if self._win:
            self._hide()
            self._show()


class _StdoutRouter(_io.TextIOBase):
    def __init__(self, q):
        self._q = q

    def write(self, msg):
        self._q.put(msg)
        return len(msg)

    def flush(self):
        pass


class WaybackGUI:
    # Pixels of indent per child level in the Element selector rows.
    _INDENT_PX = 16

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()   # hide until centred to avoid position flicker
        self.root.title("Wayback Element Tracker")
        self.root.minsize(700, 640)

        # -- Modern Windows look -----------------------------------------------
        # Use the vista theme (native Win32 visual styles) on Windows;
        # fall back to clam on other platforms.
        _style = ttk.Style(self.root)
        for _t in ("vista", "clam"):
            try:
                _style.theme_use(_t)
                break
            except tk.TclError:
                pass

        # Set default font to Segoe UI (Windows 10/11 standard UI font).
        # TkFixedFont (used by the log widget) keeps Consolas.
        for _fname, _family in (
            ("TkDefaultFont",  "Segoe UI"),
            ("TkTextFont",     "Segoe UI"),
            ("TkHeadingFont",  "Segoe UI"),
            ("TkCaptionFont",  "Segoe UI"),
            ("TkSmallCaptionFont", "Segoe UI"),
            ("TkFixedFont",    "Consolas"),
        ):
            try:
                _tkfont.nametofont(_fname).configure(family=_family, size=9)
            except Exception:
                pass
        # Use the Python interpreter's own icon (Python logo) on Windows.
        # Falls back to clearing the Tk feather, then silently gives up.
        try:
            self.root.iconbitmap(sys.executable)
        except Exception:
            try:
                self.root.iconbitmap(default="")
            except Exception:
                pass

        self._vars = {}
        self._field_rows = {}
        # Dynamic element slot UI:
        #   _active_slots                 -> ordered list of active slot IDs
        #   _next_slot                    -> next slot ID to allocate
        #   _element_levels[sid]          -> list of level dicts for slot sid
        #   _element_level0_containers[sid] -> frame holding ONLY the level-0 row
        #   _element_containers[sid]      -> frame holding child rows (levels 1+)
        #   _element_add_btns[sid]        -> "+ Add Child" button for slot sid
        #   _element_add_spacers[sid]     -> indent spacer frame for slot sid
        #   _element_section_frames[sid]  -> {"card", "header_lbl", "sep", "remove_btn"}
        self._active_slots              = []
        self._next_slot                 = 1
        self._element_levels            = {}
        self._element_level0_containers = {}
        self._element_containers        = {}
        self._element_add_btns          = {}
        self._element_add_spacers       = {}
        self._element_section_frames    = {}
        # Dynamic pair slot UI (reformat tab):
        #   _pair_entries      -> list of {lvar, vvar, lframe, vframe, le, ve} dicts
        #   _pairs_l_container -> Frame holding L-entry columns
        #   _pairs_v_container -> Frame holding V-entry columns
        #   _pairs_add_btn     -> "+" button
        #   _pairs_rem_btn     -> "−" remove-last-pair button
        #   _pairs_lbl_widgets -> [Label Elements label, Value Elements label]
        #   _pairs_qbtns       -> [? btn for label_elements, ? btn for value_elements]
        self._pair_entries           = []
        self._pairs_l_container      = None
        self._pairs_v_container      = None
        self._pairs_add_btn          = None
        self._pairs_rem_btn          = None
        self._pairs_add_wrap         = None
        self._pairs_rem_wrap         = None
        self._pairs_lbl_widgets      = []
        self._pairs_qbtns            = []
        self._running = False
        self._proc = None
        self._run_thread = None
        self._log_q = _queue.Queue()
        self._bound_shortcuts = {}   # action -> (sequence, func_id)
        self._switching_tab_kb = False   # suppresses TabChangedHandler focus-move on keyboard tab switch
        # Progress canvas state
        self._prog_mode    = "idle"  # "idle" | "wrap" | "fill"
        self._prog_total   = 0       # total snapshots expected
        self._prog_done    = 0       # count of completed snapshots
        self._wrap_pos     = 0.0     # current pixel position of wrap segment (float)
        self._wrap_job     = None    # root.after() handle for wrap animation tick

        self._defaults = {}
        for line in DEFAULT_SETTINGS.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            self._defaults[k.strip().lower()] = v.strip()
        # seed extract defaults for any element_N in DEFAULT_SETTINGS
        for _dk in list(self._defaults):
            if re.match(r'^element_\d+$', _dk):
                _dn = _dk[len("element_"):]
                self._defaults.setdefault(f"extract_{_dn}", "text")

        # Unsaved-changes tracking; _loading suppresses dirty-marking and
        # _update_states during bulk var changes (load / reset).
        self._loading = True
        self._unsaved = False
        self._disabled_real_values = {}   # key -> real value while field is disabled

        self._build_ui()
        self._load_from_file()   # sets _loading=False and calls _update_states()
        self._rebind_shortcuts() # re-register now that vars hold their loaded values
        for key in ("frequency", "headless_browser", "reformat", "split_output", "label_merge"):
            self._var(key).trace_add("write", self._update_states)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Centre on screen now that the window has a real size
        self.root.update_idletasks()
        w  = self.root.winfo_reqwidth()
        h  = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        self.root.deiconify()   # show now that position is set

        self._poll_log()
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    # -- Variable helpers ------------------------------------------------------
    def _var(self, key, default=""):
        if key not in self._vars:
            sv = tk.StringVar(value=default)
            sv.trace_add("write", self._mark_dirty)
            self._vars[key] = sv
        return self._vars[key]

    def _mark_dirty(self, *_):
        if not self._loading:
            self._unsaved = True
            # Schedule a check - if the user reverted back to the saved state
            # (e.g. enabled then disabled a setting), clear the dirty flag.
            self.root.after_idle(self._check_if_clean)

    def _check_if_clean(self):
        """Clear _unsaved if current state matches the last saved/loaded snapshot."""
        if self._loading or not hasattr(self, "_saved_raw"):
            return
        current = {k: self._disabled_real_values.get(k, sv.get())
                   for k, sv in self._vars.items()
                   if not re.match(r'^(element|extract)_\d+$', k)}
        for new_num, slot_id in enumerate(self._active_slots, 1):
            current[f"element_{new_num}"] = self._element_get_value(slot_id)
            extract = self._vars.get(f"extract_{slot_id}")
            current[f"extract_{new_num}"] = extract.get() if extract else "text"
        current["_slot_count"] = len(self._active_slots)
        if current == self._saved_raw:
            self._unsaved = False

    def _save_snapshot(self):
        """Capture the current true state for future dirty-checking."""
        # Exclude element/extract vars — stale slot vars from removed cards linger
        # as empty strings and would cause false dirty hits. Use the canonical
        # renumbered representation instead, mirroring what _check_if_clean does.
        self._saved_raw = {k: self._disabled_real_values.get(k, sv.get())
                           for k, sv in self._vars.items()
                           if not re.match(r'^(element|extract)_\d+$', k)}
        for new_num, slot_id in enumerate(self._active_slots, 1):
            self._saved_raw[f"element_{new_num}"] = self._element_get_value(slot_id)
            extract = self._vars.get(f"extract_{slot_id}")
            self._saved_raw[f"extract_{new_num}"] = extract.get() if extract else "text"
        self._saved_raw["_slot_count"] = len(self._active_slots)

    # -- Layout helpers --------------------------------------------------------
    def _scrollable_tab(self, title):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=title)

        # Use grid on outer so the horizontal scrollbar spans the full width
        # (including the column occupied by the vertical scrollbar).
        # Layout:  col 0 = canvas, col 1 = vsb
        #          row 0 = canvas/vsb, row 1 = hsb (spans both columns)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical",   command=canvas.yview)
        hsb = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        inner = ttk.Frame(canvas)

        cw = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Canvas always in (row=0, col=0); scrollbars shown on demand via grid/grid_remove
        canvas.grid(row=0, column=0, sticky="nsew")

        def _wheel(e):
            # Only scroll vertically if content is taller than the viewport
            if inner.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-1 * (e.delta / 120)) * 2, "units")

        def _hwheel(e):
            # Only scroll horizontally if content is wider than the viewport
            if inner.winfo_reqwidth() > canvas.winfo_width():
                canvas.xview_scroll(int(-1 * (e.delta / 120)) * 2, "units")

        def _combo_wheel(e):
            # Scroll the canvas, and block the combobox from cycling its value
            _wheel(e)
            return "break"

        _wheel_bound = set()

        def _bind_wheel(widget):
            wid = str(widget)
            if wid not in _wheel_bound:
                _wheel_bound.add(wid)
                if isinstance(widget, ttk.Combobox):
                    widget.bind("<MouseWheel>",       _combo_wheel, add="+")
                else:
                    widget.bind("<MouseWheel>",       _wheel,  add="+")
                    widget.bind("<Shift-MouseWheel>", _hwheel, add="+")
            for child in widget.winfo_children():
                _bind_wheel(child)

        canvas.bind("<MouseWheel>",       _wheel)
        canvas.bind("<Shift-MouseWheel>", _hwheel)

        _vsb_shown = [False]
        _hsb_shown = [False]

        def _check_scrollbars():
            need_v = inner.winfo_reqheight() > canvas.winfo_height()
            need_h = inner.winfo_reqwidth()  > canvas.winfo_width()
            if need_v != _vsb_shown[0]:
                _vsb_shown[0] = need_v
                if need_v:
                    vsb.grid(row=0, column=1, sticky="ns")
                else:
                    vsb.grid_remove()
            if need_h != _hsb_shown[0]:
                _hsb_shown[0] = need_h
                if need_h:
                    # columnspan=2 so it runs under both the canvas and the vsb
                    hsb.grid(row=1, column=0, columnspan=2, sticky="ew")
                else:
                    hsb.grid_remove()

        def _trigger_scroll_check():
            """Force-update the canvas window width and recheck scrollbars.

            Call this whenever content inside inner may have grown beyond the
            canvas width in a way that doesn't cause inner's own <Configure>
            to fire (e.g. a sticky='w' child growing inside a weight-1 column).
            """
            req_w = inner.winfo_reqwidth()
            c_w   = canvas.winfo_width()
            new_w = max(c_w, req_w) if c_w > 1 else req_w
            canvas.itemconfig(cw, width=new_w)
            canvas.configure(scrollregion=canvas.bbox("all"))
            _check_scrollbars()

        # Attach to inner so callers that only hold a reference to inner can
        # trigger a scroll update without knowing about the canvas internals.
        inner._trigger_scroll_check = _trigger_scroll_check

        def _scroll_to_show_right():
            """Scroll horizontally so the right edge of inner's content is visible.

            Called after a new pair entry is added so the new box is immediately
            visible without the user having to scroll manually.
            """
            # Force all pending geometry passes to complete first — specifically
            # the ? button repack — before measuring the scrollregion, otherwise
            # winfo_reqwidth() stops at the last entry and clips the ? button.
            canvas.update_idletasks()
            _trigger_scroll_check()
            canvas.xview_moveto(1.0)

        inner._scroll_to_show_right = _scroll_to_show_right

        def _on_inner_configure(e):
            req_w = inner.winfo_reqwidth()
            c_w   = canvas.winfo_width()
            canvas.itemconfig(cw, width=max(c_w, req_w) if c_w > 1 else req_w)
            canvas.configure(scrollregion=canvas.bbox("all"))
            _bind_wheel(inner)
            canvas.after_idle(_check_scrollbars)

        def _on_canvas_configure(e):
            # Expand inner to fill the canvas width, but never shrink it below
            # its own requested width so horizontally-growing content is preserved.
            canvas.itemconfig(cw, width=max(e.width, inner.winfo_reqwidth()))
            canvas.after_idle(_check_scrollbars)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # On first display the canvas sets its window width before the Text
        # widgets inside have completed their initial layout pass, so they
        # don't wrap correctly until the next configure cycle.  Scheduling a
        # second pass after idle ensures the correct width is applied on load.
        def _force_reflow():
            canvas.itemconfig(cw, width=max(canvas.winfo_width(),
                                            inner.winfo_reqwidth()))
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.after_idle(_force_reflow)

        return inner

    def _section(self, parent, row, text):
        ttk.Label(parent, text=text, font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 2))

    def _sep(self, parent, row):
        ttk.Separator(parent, orient="horizontal").grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=8, pady=4)

    @staticmethod
    def _in_canvas_viewport(widget):
        """Return True only if widget is within its scrollable canvas's visible area."""
        w = widget.master
        while w is not None:
            if isinstance(w, tk.Canvas):
                wy = widget.winfo_rooty()
                cy = w.winfo_rooty()
                return cy <= wy and (wy + widget.winfo_height()) <= (cy + w.winfo_height())
            w = getattr(w, "master", None)
        return True  # no canvas ancestor — always visible

    def _qbtn(self, parent, row, tip_key):
        """Place a ? button in column 3; attach tooltip from _TIPS. Returns button."""
        tip = _TIPS.get(tip_key, "")
        if not tip:
            return None
        btn = ttk.Button(parent, text="?", width=2)
        btn.grid(row=row, column=3, sticky="nw", padx=(2, 8), pady=2)
        tt = _Tooltip(btn, tip)

        # Show/hide tooltip on keyboard focus, but only when the button is
        # actually visible (not scrolled out of view in the tab's canvas).
        btn.bind("<FocusIn>",
                 lambda e: tt._show(e) if self._in_canvas_viewport(btn) else None,
                 add="+")
        btn.bind("<FocusOut>", tt._hide, add="+")
        return btn

    def _field(self, parent, row, label, widget_fn, hint="", tip_key=""):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=0, sticky="w", padx=(10, 4), pady=3)
        w = widget_fn(parent)
        w.grid(row=row, column=1, sticky="ew", padx=(0, 4), pady=3)
        if hint:
            ttk.Label(parent, text=hint, foreground="gray").grid(
                row=row, column=2, sticky="w", padx=(0, 2))
        btn = self._qbtn(parent, row, tip_key) if tip_key else None
        if tip_key:
            # Attach silent tooltips to the widget and label; text is filled in
            # by _set_state when the field is disabled so hovering shows the reason.
            wtt  = _Tooltip(w,   "", follow_cursor=True)
            lbltt = _Tooltip(lbl, "", follow_cursor=True)
            self._field_rows[tip_key] = {
                "label": lbl, "widget": w, "qbtn": btn,
                "normal_state": w.cget("state"),
                "widget_tt": wtt, "label_tt": lbltt,
            }
        return w

    def _entry(self, parent, key, width=38):
        e = ttk.Entry(parent, textvariable=self._var(key), width=width)
        self._bind_arrow_nav(e)
        self._bind_entry_undo(e, key)
        self._bind_smooth_drag_scroll(e)
        return e

    def _bind_smooth_drag_scroll(self, entry):
        """Replace the default B1-Motion auto-scroll with a smooth, symmetric version.

        Tk's built-in behaviour scrolls left slowly (one char per timer tick) but
        teleports on the right because it repositions the view differently. This
        override handles both sides identically: the view scrolls one character per
        tick at minimum and speeds up the further outside the widget the cursor is.
        """
        _job  = [None]
        _last = [0]      # last known mouse-x relative to widget

        def _cancel():
            if _job[0]:
                entry.after_cancel(_job[0])
                _job[0] = None

        def _step():
            _job[0] = None
            x = _last[0]
            w = entry.winfo_width()
            if x < 0:
                units = max(1, (-x) // 8)
                entry.xview_scroll(-units, "units")
                idx = entry.index("@0")
            elif x >= w:
                units = max(1, (x - w + 1) // 8)
                entry.xview_scroll(units, "units")
                idx = entry.index(f"@{w - 1}")
            else:
                return   # cursor moved back inside — stop
            entry.icursor(idx)
            entry.selection_to(idx)
            _job[0] = entry.after(16, _step)

        def _on_motion(e):
            _last[0] = e.x
            w = entry.winfo_width()
            # Clamp to widget interior for the @x index so it's always valid
            clamped = max(0, min(e.x, w - 1))
            idx = entry.index(f"@{clamped}")
            entry.icursor(idx)
            entry.selection_to(idx)
            if e.x < 0 or e.x >= w:
                if not _job[0]:
                    _job[0] = entry.after(16, _step)
            else:
                _cancel()
            return "break"   # suppress Tk's class-level handler

        entry.bind("<B1-Motion>",       _on_motion)
        entry.bind("<ButtonRelease-1>", lambda e: _cancel(), add="+")

    def _bind_entry_undo(self, entry, key_or_var):
        """Attach a debounced undo/redo stack to a ttk.Entry widget.
        Changes are committed to the undo stack ~400 ms after the last keystroke,
        so Ctrl+Z steps back in meaningful chunks rather than character by character.
        Loading/resetting settings clears the stack so stale history isn't replayed.
        Accepts either a named settings key (str) or a bare tk.StringVar."""
        sv = self._var(key_or_var) if isinstance(key_or_var, str) else key_or_var

        # Select-all on FocusIn is the default for both Entry and Combobox;
        # clear it on the next tick so tabbing in doesn't highlight the entire value.
        entry.bind("<FocusIn>",
                   lambda e: entry.after(0, entry.selection_clear), add="+")

        undo_stack = []
        redo_stack = []
        _timer   = [None]
        _baseline = [sv.get()]   # last committed value

        def _commit():
            _timer[0] = None
            current = sv.get()
            if current != _baseline[0]:
                undo_stack.append(_baseline[0])
                redo_stack.clear()
                _baseline[0] = current

        def _on_change(*_):
            if self._loading:
                # Settings load/reset: update baseline silently and wipe history.
                if _timer[0]:
                    entry.after_cancel(_timer[0])
                    _timer[0] = None
                _baseline[0] = sv.get()
                undo_stack.clear()
                redo_stack.clear()
                return
            if _timer[0]:
                entry.after_cancel(_timer[0])
            _timer[0] = entry.after(400, _commit)

        def _undo(e):
            # Flush any pending debounce first so the current edit is on the stack.
            if _timer[0]:
                entry.after_cancel(_timer[0])
                _commit()
            if undo_stack:
                redo_stack.append(sv.get())
                val = undo_stack.pop()
                _baseline[0] = val
                sv.set(val)
                entry.icursor("end")
            return "break"

        def _redo(e):
            if redo_stack:
                undo_stack.append(sv.get())
                val = redo_stack.pop()
                _baseline[0] = val
                sv.set(val)
                entry.icursor("end")
            return "break"

        sv.trace_add("write", _on_change)
        entry.bind("<Control-z>",       _undo)
        entry.bind("<Control-y>",       _redo)
        entry.bind("<Control-Shift-z>", _redo)

    def _bind_arrow_nav(self, widget):
        """Bind Up/Down arrows to move the cursor to the start/end of the field
        and scroll the entry view so the cursor is always visible.

        Also override Left/Right so both sides scroll exactly one character at a
        time.  Tk's default Right-arrow handler repositions the view by a large
        offset (making it feel like it teleports), while Left already scrolls one
        character per press.  Shift variants extend the selection by one character.
        """
        def _up(e):
            widget.icursor(0)
            widget.xview_moveto(0)
            return "break"
        def _down(e):
            widget.icursor("end")
            widget.xview_moveto(1)
            return "break"

        def _left(e):
            pos = widget.index("insert")
            if pos == 0:
                return "break"
            new_pos = pos - 1
            widget.icursor(new_pos)
            # If the cursor has gone past the left edge, scroll left by 1 unit
            if widget.index("@0") > new_pos:
                widget.xview_scroll(-1, "units")
            return "break"

        def _right(e):
            pos = widget.index("insert")
            end = widget.index("end")
            if pos >= end:
                return "break"
            new_pos = pos + 1
            widget.icursor(new_pos)
            # If the cursor has gone past the right edge, scroll right by 1 unit
            w = widget.winfo_width()
            if widget.index(f"@{w - 1}") < new_pos:
                widget.xview_scroll(1, "units")
            return "break"

        def _shift_left(e):
            ins = widget.index("insert")
            new_pos = max(0, ins - 1)
            widget.icursor(new_pos)
            widget.selection_to(new_pos)
            if widget.index("@0") > new_pos:
                widget.xview_scroll(-1, "units")
            return "break"

        def _shift_right(e):
            ins = widget.index("insert")
            end = widget.index("end")
            new_pos = min(end, ins + 1)
            widget.icursor(new_pos)
            widget.selection_to(new_pos)
            w = widget.winfo_width()
            if widget.index(f"@{w - 1}") < new_pos:
                widget.xview_scroll(1, "units")
            return "break"

        widget.bind("<Up>",          _up)
        widget.bind("<Down>",        _down)
        widget.bind("<Left>",        _left)
        widget.bind("<Right>",       _right)
        widget.bind("<Shift-Left>",  _shift_left)
        widget.bind("<Shift-Right>", _shift_right)

    def _combo(self, parent, key, values, width=14, editable=False):
        cb = ttk.Combobox(parent, textvariable=self._var(key), values=values,
                          state="normal" if editable else "readonly", width=width)
        if editable:
            self._bind_entry_undo(cb, key)
        return cb

    # -- Tab builders ----------------------------------------------------------
    def _set_state(self, key, disabled, reason="", display=None):
        row = self._field_rows.get(key)
        if not row:
            return
        w, lbl, btn = row["widget"], row["label"], row["qbtn"]
        # Values to display when a field is disabled (may differ from the default).
        # display= overrides the per-key defaults when provided.
        _disabled_display = {"threads": "1"}

        # Update the hover tooltips on the widget and label.
        # When disabled: show the reason; when enabled: clear (tooltip won't appear).
        # If the locked display value is "yes", the field is effectively enabled by the reason.
        if disabled and reason:
            _disabled_display_check = {"threads": "1"}
            shown_val = display if display is not None else _disabled_display_check.get(key, self._defaults.get(key, ""))
            state_word = "Enabled" if shown_val == "yes" else "Disabled"
            tip_text = f"{state_word} because {reason}"
        else:
            tip_text = ""
        for tt_key in ("widget_tt", "label_tt"):
            tt = row.get(tt_key)
            if tt:
                tt.update(tip_text)

        if disabled:
            # Save the real value (if not already saved from a previous disable)
            # then display the default so disabled fields don't show stale/confusing values.
            if key not in self._disabled_real_values:
                self._disabled_real_values[key] = self._var(key).get()
            self._loading = True
            shown = display if display is not None else _disabled_display.get(key, self._defaults.get(key, ""))
            self._var(key).set(shown)
            self._loading = False
            w.configure(state="disabled")
            lbl.configure(foreground="gray")
            if btn:
                btn.configure(state="disabled")
        else:
            # Restore the real value that was saved when the field was disabled.
            if key in self._disabled_real_values:
                self._loading = True
                self._var(key).set(self._disabled_real_values.pop(key))
                self._loading = False
            w.configure(state=row["normal_state"])
            lbl.configure(foreground="")
            if btn:
                btn.configure(state="normal")

    def _update_states(self, *_):
        if self._loading:
            return
        freq_all    = self._var("frequency").get() == "all"
        headless    = self._var("headless_browser").get() == "yes"
        no_reformat = self._var("reformat").get() == "no"
        split_files = self._var("split_output").get() == "files"
        not_merged  = self._var("split_output").get() != "merged"

        self._set_state("sample_from",           freq_all,    "frequency = all")
        self._set_state("result_padding",         freq_all,    "frequency = all")
        self._set_state("min_gap",                freq_all,    "frequency = all")
        self._set_state("fallback_candidates",    freq_all,    "frequency = all")
        self._set_state("threads",                headless,    "headless_browser = yes")
        self._set_state("sort",                   no_reformat, "reformat = no")
        self._set_pairs_state(no_reformat)
        self._set_state("zero_fill",              no_reformat, "reformat = no")
        self._set_state("fill_first",             no_reformat, "reformat = no")
        self._set_state("label_merge",            no_reformat, "reformat = no")
        self._set_state("label_case",             no_reformat, "reformat = no")

        # label_strip_separators: locked to yes when label_merge = yes, else normal
        merge_on = not no_reformat and self._var("label_merge").get() == "yes"
        if merge_on:
            self._set_state("label_strip_separators", True, "label_merge = yes", display="yes")
        else:
            self._set_state("label_strip_separators", no_reformat, "reformat = no")

        if freq_all and split_files:
            cp_reason = "frequency = all and split_output = files"
        elif freq_all:
            cp_reason = "frequency = all"
        else:
            cp_reason = "split_output = files"
        self._set_state("collision_priority", freq_all or split_files, cp_reason)

        if no_reformat and not_merged:
            mm_reason = "reformat = no and split_output \u2260 merged"
        elif no_reformat:
            mm_reason = "reformat = no"
        else:
            mm_reason = "split_output \u2260 merged"
        self._set_state("merged_meta", no_reformat or not_merged, mm_reason)

    def _build_url_tab(self):
        f = self._scrollable_tab("URL")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "URL"); r += 1
        self._field(f, r, "URL",
                    lambda p: self._entry(p, "url", 48),
                    tip_key="url"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Filters"); r += 1
        self._field(f, r, "Filter Any",
                    lambda p: self._entry(p, "filter_any", 48),
                    tip_key="filter_any"); r += 1
        self._field(f, r, "Filter All",
                    lambda p: self._entry(p, "filter_all", 48),
                    tip_key="filter_all"); r += 1
        self._field(f, r, "Case Sensitive",
                    lambda p: self._combo(p, "case_sensitive", YES_NO),
                    tip_key="case_sensitive"); r += 1
        self._field(f, r, "Match Child Paths",
                    lambda p: self._combo(p, "match_child_paths", YES_NO),
                    tip_key="match_child_paths"); r += 1

    # -- Element level helpers ---------------------------------------------------

    @staticmethod
    def _split_element_levels(raw):
        """Split a saved element string back into individual level tokens.

        Each token is either a full HTML opening tag (optionally preceded by a
        bare integer with no space, e.g. ``2<span class="x">``), or a bare
        integer used as a direct-child index.  The tokens are later re-joined
        with spaces to produce the value that ``wayback_element_tracker.py``
        already knows how to parse – so no changes are needed in the backend.
        """
        raw = raw.strip()
        if not raw:
            return [""]

        opening_tag_re = re.compile(r'<(?!/)[^>]+>')
        tag_matches = list(opening_tag_re.finditer(raw))

        if not tag_matches:
            # Pure bare numbers (rare edge-case)
            parts = raw.split()
            return parts if parts else [""]

        levels = []
        prev_end = 0

        for idx, m in enumerate(tag_matches):
            gap      = raw[prev_end : m.start()]
            gap_text = re.sub(r'<[^>]+>', '', gap).strip()
            tag_html = m.group()

            if idx == 0:
                levels.append(tag_html)
            else:
                nums = re.findall(r'\b(\d+)\b', gap_text)
                # Numbers that precede the tag but aren't the immediate prefix
                # are standalone bare-child steps.
                for n in nums[:-1]:
                    levels.append(n)
                if nums:
                    # The last number is glued directly to the tag (no space).
                    levels.append(nums[-1] + tag_html)
                else:
                    levels.append(tag_html)

            prev_end = m.end()

        # Bare integers after the last opening tag (nth-child navigation)
        suffix = raw[prev_end:]
        if not re.search(r'</\s*\w', suffix):        # ignore if there's a closing tag
            for n in re.findall(r'\b(\d+)\b', re.sub(r'<[^>]+>', '', suffix)):
                levels.append(n)

        return levels if levels else [""]

    def _element_get_value(self, i):
        """Return the current raw element string for slot *i* by joining its levels."""
        parts = [lv["entry"].get("1.0", "end-1c").replace("\n", " ").strip()
                 for lv in self._element_levels.get(i, [])]
        return " ".join(p for p in parts if p)

    def _add_level(self, i, value="", *, _loading=False):
        """Append one new level row to element slot *i*."""
        levels     = self._element_levels[i]
        level_idx  = len(levels)
        # Level 0 (root) lives in its own frame so the ? element button can be
        # inserted between it and the child rows in tab order.
        rows_frame = (self._element_level0_containers[i]
                      if level_idx == 0 else
                      self._element_containers[i])

        row_frame = ttk.Frame(rows_frame)
        row_frame.pack(fill="x", pady=(0, 2))
        row_frame.columnconfigure(0, minsize=level_idx * self._INDENT_PX)
        row_frame.columnconfigure(1, weight=1)

        # Indent marker for child levels.
        # Level 0 is the root – its label is created but NOT gridded so the
        # entry starts flush with other form fields.  Each successive child
        # level shows a ↳ arrow right-aligned in the fixed-width column 0,
        # giving consistent pixel-precise alignment regardless of font metrics.
        if level_idx == 0:
            indent_lbl = ttk.Label(row_frame, text="")
            # intentionally not gridded - entry occupies the full row width
        else:
            indent_lbl = ttk.Label(row_frame, text="\u21b3", foreground="#888888")
            indent_lbl.grid(row=0, column=0, sticky="e", padx=(0, 2))

        _border = ttk.Style().lookup("TEntry", "bordercolor") or "#999999"
        entry = tk.Text(row_frame, height=2, width=1, wrap=tk.WORD,
                        font=("TkDefaultFont", 9),
                        relief="flat", borderwidth=0,
                        highlightthickness=1,
                        highlightbackground=_border,
                        highlightcolor=_border,
                        padx=3, pady=2,
                        undo=True)
        entry.insert("1.0", value)
        entry.edit_modified(False)
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 2))

        # Tab / Shift-Tab move focus out instead of inserting whitespace.
        entry.bind("<Tab>",       lambda e, w=entry: (w.tk_focusNext().focus_set(), "break")[1])
        entry.bind("<Shift-Tab>", lambda e, w=entry: (w.tk_focusPrev().focus_set(), "break")[1])

        # Dirty-marking: any edit marks the settings unsaved, then checks if
        # the user reverted back to the saved state.
        def _on_level_change(e, _entry=entry, _i=i):
            if _entry.edit_modified() and not self._loading:
                self._unsaved = True
                self.root.after_idle(self._check_if_clean)
            _entry.edit_modified(False)
        entry.bind("<<Modified>>", _on_level_change)

        # Scroll interception: if this Text box has overflow content, consume
        # the wheel event here and don't let it propagate to the page canvas.
        # If the box content fits entirely (nothing to scroll), do nothing so
        # the page canvas handler (added later by _bind_wheel) fires normally.
        def _on_entry_wheel(e, _txt=entry):
            lo, hi = _txt.yview()
            scrolling_down = e.delta < 0
            scrolling_up   = e.delta > 0
            at_bottom = hi >= 1.0
            at_top    = lo <= 0.0
            if (lo > 0.0 or hi < 1.0) and not (scrolling_down and at_bottom) and not (scrolling_up and at_top):
                _txt.yview_scroll(int(-1 * (e.delta / 120)), "units")
                return "break"
        entry.bind("<MouseWheel>", _on_entry_wheel)

        # Remove button (hidden while there is only one level)
        remove_btn = ttk.Button(row_frame, text="✕", width=2)
        remove_btn.grid(row=0, column=2, sticky="e", padx=(0, 0))
        _rm_tt = _Tooltip(remove_btn, _TIPS.get("remove_child", ""))
        remove_btn.bind("<FocusIn>",
                        lambda e, b=remove_btn, tt=_rm_tt:
                            tt._show(e) if self._in_canvas_viewport(b) else None,
                        add="+")
        remove_btn.bind("<FocusOut>", _rm_tt._hide, add="+")

        def _do_remove(_i=i, _idx=level_idx):
            self._remove_level(_i, _idx)
        remove_btn.configure(command=_do_remove)

        record = {"row": row_frame, "entry": entry,
                  "remove_btn": remove_btn, "indent_lbl": indent_lbl}
        levels.append(record)

        self._refresh_level_buttons(i)

        if not _loading and not self._loading:
            self._unsaved = True
            self.root.after_idle(self._check_if_clean)

        # Let the scrollable canvas know the content height changed.
        rows_frame.event_generate("<Configure>")
        return record

    def _remove_level(self, i, level_idx):
        """Remove the level at *level_idx* from element slot *i*."""
        levels = self._element_levels[i]
        if level_idx == 0 or len(levels) <= 1:
            return   # level 0 can never be removed; always keep at least one level

        record = levels.pop(level_idx)
        record["row"].destroy()

        # Renumber the remove-button commands so indices stay correct.
        for new_idx, lv in enumerate(levels):
            def _do_remove(_i=i, _idx=new_idx):
                self._remove_level(_i, _idx)
            lv["remove_btn"].configure(command=_do_remove)
            # Update indent marker and entry width to match the new index.
            lv["row"].columnconfigure(0, minsize=new_idx * self._INDENT_PX)
            if new_idx == 0:
                lv["indent_lbl"].configure(text="")
                lv["indent_lbl"].grid_remove()
            else:
                lv["indent_lbl"].configure(text="↳")
                lv["indent_lbl"].grid(row=0, column=0, sticky="e", padx=(0, 2))

        self._refresh_level_buttons(i)
        if not self._loading:
            self._unsaved = True
            self.root.after_idle(self._check_if_clean)
        self._element_containers[i].event_generate("<Configure>")

    def _refresh_level_buttons(self, i):
        """Hide the remove button on level 0 (it can never be deleted);
        show it on all child levels.
        Also update the Add Child spacer so the button stays one indent level
        past the current deepest child."""
        levels = self._element_levels[i]
        for idx, lv in enumerate(levels):
            if idx == 0:
                lv["remove_btn"].grid_remove()
                # Level-0 remove button is never shown; exclude it from Tab
                # traversal so focus moves straight from the entry to ? element.
                lv["remove_btn"].configure(takefocus=False)
            else:
                lv["remove_btn"].grid()
        spacer = self._element_add_spacers.get(i)
        if spacer is not None:
            spacer.configure(width=len(levels) * self._INDENT_PX)

    # -- Elements tab builder ----------------------------------------------------

    def _build_elements_tab(self):
        f = self._scrollable_tab("Elements")
        f.columnconfigure(0, weight=1)

        # Container into which element cards are packed dynamically.
        self._elements_tab_inner   = f
        self._elements_container   = ttk.Frame(f)
        self._elements_container.grid(row=0, column=0, sticky="ew")
        self._elements_container.columnconfigure(0, weight=1)

        # "Add Element" button below the cards.
        add_bar = ttk.Frame(f)
        add_bar.grid(row=1, column=0, sticky="w", padx=10, pady=(4, 6))
        ttk.Button(add_bar, text="+ Add Element", width=14,
                   command=self._add_element_slot).pack(side="left")

    # -- Dynamic element slot management ----------------------------------------

    def _add_element_slot(self, parts=None, extract="text", _loading=False):
        """Append a new element card to the Elements tab and return its slot ID."""
        EXTRACT_OPTS   = ["text", "title", "href", "src", "value", "content",
                          "alt", "placeholder", "datetime", "action", "data-"]
        EXTRACT_HEIGHT = len(EXTRACT_OPTS)

        slot_id     = self._next_slot
        self._next_slot += 1
        self._active_slots.append(slot_id)
        display_num = len(self._active_slots)

        # ---- Card frame -------------------------------------------------------
        card = ttk.Frame(self._elements_container)
        card.pack(fill="x", expand=True)
        card.columnconfigure(1, weight=1)
        card_r = 0

        # Separator (not shown for the very first card; created so it can be
        # toggled when the first card is removed).
        sep_widget = ttk.Separator(card, orient="horizontal")
        sep_widget.grid(row=card_r, column=0, columnspan=4, sticky="ew",
                        padx=8, pady=4)
        card_r += 1
        if display_num == 1:
            sep_widget.grid_remove()   # hidden for first card

        # ---- Section header row ----------------------------------------------
        header_frame = ttk.Frame(card)
        header_frame.grid(row=card_r, column=0, columnspan=4, sticky="ew",
                          padx=10, pady=(8, 2))
        header_frame.columnconfigure(0, weight=1)

        header_lbl = ttk.Label(header_frame,
                               text=f"Element {display_num}",
                               font=("TkDefaultFont", 9, "bold"))
        header_lbl.grid(row=0, column=0, sticky="w")

        remove_elem_btn = ttk.Button(
            header_frame, text="Remove Element", width=14,
            command=lambda _sid=slot_id: self._remove_element_slot(_sid),
        )
        remove_elem_btn.grid(row=0, column=1, sticky="e")
        card_r += 1

        # ---- Element input rows ----------------------------------------------
        ttk.Label(card, text="Element").grid(
            row=card_r, column=0, sticky="nw", padx=(10, 4), pady=3)

        outer = ttk.Frame(card)
        outer.grid(row=card_r, column=1, sticky="ew", padx=(0, 4), pady=3)
        outer.columnconfigure(0, weight=1)

        # Level 0 ("root") row lives in its own frame so the ? element tooltip
        # button can be placed right after it in tab order — before any child
        # rows and before the + Add Child button.
        level_0_frame = ttk.Frame(outer)
        level_0_frame.pack(fill="x")
        level_0_frame.columnconfigure(1, weight=1)

        # Child rows (levels 1+) live in a separate frame that follows
        # level_0_frame in outer's widget list, so they come after ? element in
        # the tab order.
        children_frame = ttk.Frame(outer)
        children_frame.pack(fill="x")
        children_frame.columnconfigure(1, weight=1)

        self._element_levels[slot_id]              = []
        self._element_level0_containers[slot_id]   = level_0_frame
        self._element_containers[slot_id]          = children_frame

        # Create the Add Child bar and register the spacer BEFORE calling
        # _add_level, so _refresh_level_buttons can correctly set the spacer
        # width on every level added during loading.  btn_frame is packed
        # into outer only after the level rows so it appears below them.
        btn_frame  = ttk.Frame(outer)
        add_spacer = tk.Frame(btn_frame, width=self._INDENT_PX, height=1)
        add_spacer.pack(side="left")
        add_spacer.pack_propagate(False)
        self._element_add_spacers[slot_id] = add_spacer
        add_btn = ttk.Button(btn_frame, text="+  Add Child ", width=14,
                             command=lambda _sid=slot_id: self._add_level(_sid))
        add_btn.pack(side="left")
        self._element_add_btns[slot_id] = add_btn

        for part in (parts or [""]):
            self._add_level(slot_id, value=part, _loading=_loading or self._loading)

        # ? button for the element field: created after _add_level so we can
        # attach it directly to the level-0 row frame at column 3.  This places
        # it in tab order immediately after the level-0 entry and before any
        # child rows (which live in children_frame, later in outer's widget list)
        # and before + Add Child (in btn_frame, also later).
        level_0_row_frame = self._element_levels[slot_id][0]["row"]
        q_btn = ttk.Button(level_0_row_frame, text="?", width=2)
        q_btn.grid(row=0, column=3, sticky="e", padx=(2, 0))
        q_tt = _Tooltip(q_btn, _TIPS.get("element", ""))

        q_btn.bind("<FocusIn>",
                   lambda e, b=q_btn, tt=q_tt:
                       tt._show(e) if self._in_canvas_viewport(b) else None,
                   add="+")
        q_btn.bind("<FocusOut>", q_tt._hide, add="+")

        # Pack btn_frame below the level rows.
        btn_frame.pack(fill="x", pady=(2, 0))

        _add_qbtn = ttk.Button(btn_frame, text="?", width=2)
        _add_qbtn.pack(side="left", padx=(6, 0))
        _add_tt = _Tooltip(_add_qbtn, _TIPS.get("add_child", ""))

        _add_qbtn.bind("<FocusIn>",
                       lambda e, b=_add_qbtn, tt=_add_tt:
                           tt._show(e) if self._in_canvas_viewport(b) else None,
                       add="+")
        _add_qbtn.bind("<FocusOut>", _add_tt._hide, add="+")

        card_r += 1

        # ---- Extract row -----------------------------------------------------
        ttk.Label(card, text="Extract").grid(
            row=card_r, column=0, sticky="w", padx=(10, 4), pady=3)
        _ecb_frame = ttk.Frame(card)
        _ecb_frame.grid(row=card_r, column=1, columnspan=3, sticky="w",
                        padx=(0, 4), pady=3)
        _ecb = ttk.Combobox(_ecb_frame,
                            textvariable=self._var(f"extract_{slot_id}"),
                            values=EXTRACT_OPTS, state="normal",
                            width=18, height=EXTRACT_HEIGHT)
        _ecb.pack(side="left")
        _ecb.bind("<<ComboboxSelected>>",
                  lambda e, cb=_ecb: cb.selection_clear(), add="+")
        # Prevent a blank extract: reset to "text" if the user clears the field.
        def _on_extract_focusout(e, _sid=slot_id):
            if not self._var(f"extract_{_sid}").get().strip():
                self._var(f"extract_{_sid}").set("text")
        _ecb.bind("<FocusOut>", _on_extract_focusout, add="+")
        self._bind_entry_undo(_ecb, f"extract_{slot_id}")

        # Set extract value without triggering dirty-marking
        _prev_loading = self._loading
        self._loading = True
        self._var(f"extract_{slot_id}").set(extract if extract else "text")
        self._loading = _prev_loading

        _ext_qbtn = ttk.Button(_ecb_frame, text="?", width=2)
        _ext_qbtn.pack(side="left", padx=(6, 0))
        _ext_tt = _Tooltip(_ext_qbtn, _TIPS.get("extract", ""))
        _ext_qbtn.bind("<FocusIn>",
                       lambda e, b=_ext_qbtn, tt=_ext_tt:
                           tt._show(e) if self._in_canvas_viewport(b) else None,
                       add="+")
        _ext_qbtn.bind("<FocusOut>", _ext_tt._hide, add="+")
        card_r += 1

        # ---- Store card metadata --------------------------------------------
        self._element_section_frames[slot_id] = {
            "card":        card,
            "header_lbl":  header_lbl,
            "sep":         sep_widget,
            "remove_btn":  remove_elem_btn,
        }

        self._refresh_element_remove_btns()

        if not (_loading or self._loading):
            self._unsaved = True
            self.root.after_idle(self._check_if_clean)

        # Notify the scrollable canvas that content height changed.
        self._elements_container.event_generate("<Configure>")
        return slot_id

    def _remove_element_slot(self, slot_id):
        """Remove an element slot card from the Elements tab."""
        if len(self._active_slots) <= 1:
            return   # always keep at least one slot

        info = self._element_section_frames.pop(slot_id, None)
        if info:
            info["card"].destroy()

        self._active_slots.remove(slot_id)

        for d in (self._element_levels, self._element_containers,
                  self._element_level0_containers, self._element_add_btns,
                  self._element_add_spacers):
            d.pop(slot_id, None)

        # Clear the vars for this slot so they don't pollute the written file.
        for k in (f"element_{slot_id}", f"extract_{slot_id}"):
            if k in self._vars:
                _prev = self._loading
                self._loading = True
                self._vars[k].set("")
                self._loading = _prev

        self._refresh_element_headers()
        self._refresh_element_remove_btns()

        if not self._loading:
            self._unsaved = True
            self.root.after_idle(self._check_if_clean)

        self._elements_container.event_generate("<Configure>")

    def _refresh_element_headers(self):
        """Renumber section headers after an add or remove."""
        for display_num, slot_id in enumerate(self._active_slots, 1):
            info = self._element_section_frames.get(slot_id)
            if not info:
                continue
            info["header_lbl"].configure(text=f"Element {display_num}")
            sep = info.get("sep")
            if sep:
                if display_num == 1:
                    sep.grid_remove()
                else:
                    sep.grid()

    def _refresh_element_remove_btns(self):
        """Disable the Remove button when only one slot remains."""
        only_one = len(self._active_slots) <= 1
        for slot_id in self._active_slots:
            info = self._element_section_frames.get(slot_id)
            if info and "remove_btn" in info:
                info["remove_btn"].configure(
                    state="disabled" if only_one else "normal")

    def _build_date_row(self, parent, row, label_text, prefix, tip_key):
        """Build a date row with separate Year / Month / Day entry boxes.

        The three sub-vars (<prefix>_date_y/m/d) are UI-only: they are combined
        into the canonical <prefix>_date var (YYYYMMDD) via _sync_date_composite,
        which is what gets written to settings.txt.
        """
        lbl = ttk.Label(parent, text=label_text)
        lbl.grid(row=row, column=0, sticky="w", padx=(10, 4), pady=3)

        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=(0, 4), pady=3)

        y_var = self._var(f"{prefix}_date_y")
        m_var = self._var(f"{prefix}_date_m")
        d_var = self._var(f"{prefix}_date_d")

        ttk.Label(frame, text="Y").pack(side="left")
        y_entry = ttk.Entry(frame, textvariable=y_var, width=5)
        y_entry.pack(side="left", padx=(2, 8))
        self._bind_arrow_nav(y_entry)
        self._bind_smooth_drag_scroll(y_entry)

        ttk.Label(frame, text="M").pack(side="left")
        m_entry = ttk.Entry(frame, textvariable=m_var, width=3)
        m_entry.pack(side="left", padx=(2, 8))
        self._bind_arrow_nav(m_entry)
        self._bind_smooth_drag_scroll(m_entry)

        ttk.Label(frame, text="D").pack(side="left")
        d_entry = ttk.Entry(frame, textvariable=d_var, width=3)
        d_entry.pack(side="left", padx=(2, 0))
        self._bind_arrow_nav(d_entry)
        self._bind_smooth_drag_scroll(d_entry)

        if tip_key:
            tip = _TIPS.get(tip_key, "")
            if tip:
                qbtn = ttk.Button(frame, text="?", width=2)
                qbtn.pack(side="left", padx=(6, 0))
                qtt = _Tooltip(qbtn, tip)
                qbtn.bind("<FocusIn>",
                          lambda e, b=qbtn, tt=qtt:
                              tt._show(e) if self._in_canvas_viewport(b) else None,
                          add="+")
                qbtn.bind("<FocusOut>", qtt._hide, add="+")

        def _on_change(*_):
            if not self._loading:
                self._sync_date_composite(prefix)

        y_var.trace_add("write", _on_change)
        m_var.trace_add("write", _on_change)
        d_var.trace_add("write", _on_change)

    def _sync_date_composite(self, prefix):
        """Combine <prefix>_date_y/m/d into a YYYYMMDD string in <prefix>_date.

        Validation rules (any failure → combined = "", i.e. no date filter):
          - All three fields must be non-empty and contain only digits.
          - Year : 1–4 digits (zero-padded to 4 on output).
          - Month: 1–12.
          - Day  : 1–31.
        Out-of-range or non-numeric input (negatives, floats, letters) is
        silently treated as "no filter" so the tracker never sees a bad value.
        """
        y = self._var(f"{prefix}_date_y").get().strip()
        m = self._var(f"{prefix}_date_m").get().strip()
        d = self._var(f"{prefix}_date_d").get().strip()

        combined = ""
        if y and m and d:
            if y.isdigit() and m.isdigit() and d.isdigit() and 1 <= len(y) <= 4:
                yi, mi, di = int(y), int(m), int(d)
                if 1 <= mi <= 12 and 1 <= di <= 31:
                    combined = f"{yi:04d}{mi:02d}{di:02d}"

        if self._var(f"{prefix}_date").get() != combined:
            self._var(f"{prefix}_date").set(combined)

    def _split_date_composite(self, prefix):
        """Split <prefix>_date (YYYYMMDD) back into the three sub-vars."""
        val = self._var(f"{prefix}_date").get().strip()
        if val and len(val) == 8 and val.isdigit():
            y, m, d = val[:4], val[4:6], val[6:8]
        else:
            y, m, d = "", "", ""
        self._var(f"{prefix}_date_y").set(y)
        self._var(f"{prefix}_date_m").set(m)
        self._var(f"{prefix}_date_d").set(d)

    def _build_schedule_tab(self):
        f = self._scrollable_tab("Schedule")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Date Range"); r += 1
        self._build_date_row(f, r, "From Date", "from", "from_date"); r += 1
        self._build_date_row(f, r, "To Date",   "to",   "to_date");   r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Snapshot Frequency"); r += 1
        self._field(f, r, "Frequency",
                    lambda p: self._combo(p, "frequency",
                    ["all", "hourly", "daily", "weekly", "monthly", "yearly"]),
                    tip_key="frequency"); r += 1
        self._field(f, r, "Sample From",
                    lambda p: self._combo(p, "sample_from", ["start", "middle", "end"]),
                    tip_key="sample_from"); r += 1
        self._field(f, r, "Collision Priority",
                    lambda p: self._combo(p, "collision_priority", ["time", "filter"]),
                    tip_key="collision_priority"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Date & Time Format"); r += 1
        self._field(f, r, "Convention",
                    lambda p: self._combo(p, "convention", ["us", "european"]),
                    tip_key="convention"); r += 1
        self._field(f, r, "Date Style",
                    lambda p: self._combo(p, "date_style", ["long", "short", "numeric"]),
                    tip_key="date_style"); r += 1
        self._field(f, r, "Year Digits",
                    lambda p: self._combo(p, "year_digits", ["4", "2"]),
                    tip_key="year_digits"); r += 1
        self._field(f, r, "Date Padding",
                    lambda p: self._combo(p, "date_padding", YES_NO),
                    tip_key="date_padding"); r += 1
        self._field(f, r, "Time Format",
                    lambda p: self._combo(p, "time_format", ["12h", "24h"]),
                    tip_key="time_format"); r += 1
        self._field(f, r, "Time Padding",
                    lambda p: self._combo(p, "time_padding", YES_NO),
                    tip_key="time_padding"); r += 1
        self._field(f, r, "Show Seconds",
                    lambda p: self._combo(p, "show_seconds", YES_NO),
                    tip_key="show_seconds"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Show in CSV"); r += 1
        self._field(f, r, "Show Month",
                    lambda p: self._combo(p, "show_month", YES_NO),
                    tip_key="show_month"); r += 1
        self._field(f, r, "Show Day",
                    lambda p: self._combo(p, "show_day", YES_NO),
                    tip_key="show_day"); r += 1
        self._field(f, r, "Show Year",
                    lambda p: self._combo(p, "show_year", YES_NO),
                    tip_key="show_year"); r += 1
        self._field(f, r, "Show Time",
                    lambda p: self._combo(p, "show_time", YES_NO),
                    tip_key="show_time"); r += 1

    def _build_output_tab(self):
        f = self._scrollable_tab("Output")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Output File"); r += 1
        self._field(f, r, "Output",
                    lambda p: self._entry(p, "output", 36),
                    tip_key="output"); r += 1
        self._field(f, r, "File Override",
                    lambda p: self._combo(p, "file_override", YES_NO),
                    tip_key="file_override"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "CSV Layout"); r += 1
        self._field(f, r, "CSV Layout",
                    lambda p: self._combo(p, "csv_layout", ["rows", "columns"]),
                    tip_key="csv_layout"); r += 1
        self._field(f, r, "Result Padding",
                    lambda p: self._combo(p, "result_padding", YES_NO),
                    tip_key="result_padding"); r += 1
        self._field(f, r, "Split Output",
                    lambda p: self._combo(p, "split_output", ["no", "files", "merged"]),
                    tip_key="split_output"); r += 1

    # -- Reformat pairs widget --------------------------------------------------

    def _build_pairs_rows(self, parent, row_l, row_v, row_btn):
        """Build the Label Elements, Value Elements, and pair-control button rows.

        ? buttons are packed inside the entry containers as the last item so
        they always sit just to the right of the final entry box and move as
        pairs are added or removed.  +/- buttons live in a dedicated fixed row
        (row_btn) and are pixel-sized to match the entry boxes after layout.
        """
        # --- Label Elements row ---
        lbl_label = ttk.Label(parent, text="Label Elements")
        lbl_label.grid(row=row_l, column=0, sticky="w", padx=(10, 4), pady=3)

        self._pairs_l_container = ttk.Frame(parent)
        self._pairs_l_container.grid(row=row_l, column=1, columnspan=3,
                                      sticky="w", padx=(0, 4), pady=3)

        # When entries are added/removed the container grows/shrinks.  inner's
        # own <Configure> won't fire (canvas window width is fixed), so we bind
        # directly to the container and nudge the scroll system ourselves.
        def _on_pairs_resize(e):
            if hasattr(parent, "_trigger_scroll_check"):
                parent._trigger_scroll_check()
        self._pairs_l_container.bind("<Configure>", _on_pairs_resize, add="+")

        # ? button lives inside the container so it trails the last entry.
        l_qbtn = ttk.Button(self._pairs_l_container, text="?", width=2)
        l_qbtn.pack(side="left", padx=(4, 0))
        l_qtt = _Tooltip(l_qbtn, _TIPS.get("label_elements", ""))
        l_qbtn.bind("<FocusIn>",
                    lambda e, b=l_qbtn, tt=l_qtt:
                        tt._show(e) if self._in_canvas_viewport(b) else None,
                    add="+")
        l_qbtn.bind("<FocusOut>", l_qtt._hide, add="+")

        # --- Value Elements row ---
        val_label = ttk.Label(parent, text="Value Elements")
        val_label.grid(row=row_v, column=0, sticky="w", padx=(10, 4), pady=3)

        self._pairs_v_container = ttk.Frame(parent)
        self._pairs_v_container.grid(row=row_v, column=1, columnspan=3,
                                      sticky="w", padx=(0, 4), pady=3)

        v_qbtn = ttk.Button(self._pairs_v_container, text="?", width=2)
        v_qbtn.pack(side="left", padx=(4, 0))
        v_qtt = _Tooltip(v_qbtn, _TIPS.get("value_elements", ""))
        v_qbtn.bind("<FocusIn>",
                    lambda e, b=v_qbtn, tt=v_qtt:
                        tt._show(e) if self._in_canvas_viewport(b) else None,
                    add="+")
        v_qbtn.bind("<FocusOut>", v_qtt._hide, add="+")

        # --- Fixed button row (+ −) ---
        # Wrapper tk.Frames let us set an explicit pixel width so they match
        # the entry boxes exactly.  pack_propagate(False) stops the frame from
        # shrinking to fit the button child.
        btn_outer = ttk.Frame(parent)
        btn_outer.grid(row=row_btn, column=1, columnspan=3, sticky="w",
                        padx=(0, 4), pady=(0, 4))

        add_wrap = tk.Frame(btn_outer)          # plain tk.Frame for pixel sizing
        add_wrap.pack_propagate(False)
        add_wrap.pack(side="left", padx=(0, 2))
        self._pairs_add_btn = ttk.Button(add_wrap, text="+",
                                          command=self._add_pair_slot)
        self._pairs_add_btn.pack(fill="both", expand=True)
        _pairs_add_tt = _Tooltip(self._pairs_add_btn, _TIPS.get("pairs_add", ""))
        self._pairs_add_btn.bind("<FocusIn>",
                                 lambda e, b=self._pairs_add_btn, tt=_pairs_add_tt:
                                     tt._show(e) if self._in_canvas_viewport(b) else None,
                                 add="+")
        self._pairs_add_btn.bind("<FocusOut>", _pairs_add_tt._hide, add="+")

        rem_wrap = tk.Frame(btn_outer)
        rem_wrap.pack_propagate(False)
        rem_wrap.pack(side="left", padx=(0, 0))
        self._pairs_rem_btn = ttk.Button(rem_wrap, text="\u2212",
                                          command=self._remove_last_pair)
        self._pairs_rem_btn.pack(fill="both", expand=True)
        _pairs_rem_tt = _Tooltip(self._pairs_rem_btn, _TIPS.get("pairs_remove", ""))
        self._pairs_rem_btn.bind("<FocusIn>",
                                 lambda e, b=self._pairs_rem_btn, tt=_pairs_rem_tt:
                                     tt._show(e) if self._in_canvas_viewport(b) else None,
                                 add="+")
        self._pairs_rem_btn.bind("<FocusOut>", _pairs_rem_tt._hide, add="+")

        # Store the wrapper frames so _sync_pair_btn_size can resize them.
        self._pairs_add_wrap = add_wrap
        self._pairs_rem_wrap = rem_wrap

        # Store refs for enable/disable and ? label updates
        self._pairs_lbl_widgets = [lbl_label, val_label]
        self._pairs_qbtns       = [l_qbtn, v_qbtn]

    def _add_pair_slot(self, lval="", vval=""):
        """Append a new entry to both the L and V rows."""
        lvar = tk.StringVar(value=str(lval))
        vvar = tk.StringVar(value=str(vval))

        # Temporarily unpack the ? buttons so the new entry frames pack before them.
        l_qbtn, v_qbtn = (self._pairs_qbtns or [None, None])
        if l_qbtn:
            l_qbtn.pack_forget()
        if v_qbtn:
            v_qbtn.pack_forget()

        lframe = ttk.Frame(self._pairs_l_container)
        lframe.pack(side="left", padx=(0, 2))
        le = ttk.Entry(lframe, textvariable=lvar, width=3)
        le.pack()
        le.bind("<FocusIn>",
                lambda e: le.after(0, le.selection_clear), add="+")

        vframe = ttk.Frame(self._pairs_v_container)
        vframe.pack(side="left", padx=(0, 2))
        ve = ttk.Entry(vframe, textvariable=vvar, width=3)
        ve.pack()
        ve.bind("<FocusIn>",
                lambda e: ve.after(0, ve.selection_clear), add="+")

        # Repack the ? buttons so they trail the last entry.
        # lift() moves each button to the top of its container's stacking order,
        # which is what tk_focusNext uses — so the entries are always traversed
        # before the ? button regardless of how many pairs exist.
        if l_qbtn:
            l_qbtn.pack(side="left", padx=(4, 0))
            l_qbtn.lift()
        if v_qbtn:
            v_qbtn.pack(side="left", padx=(4, 0))
            v_qbtn.lift()

        slot = {"lvar": lvar, "vvar": vvar, "lframe": lframe, "vframe": vframe,
                "le": le, "ve": ve}
        self._pair_entries.append(slot)
        self._bind_pair_entry_nav(slot)

        lvar.trace_add("write", lambda *_: self._sync_pairs_to_vars())
        vvar.trace_add("write", lambda *_: self._sync_pairs_to_vars())

        self._update_rem_btn()
        self._update_add_btn()
        # Pixel-sync the +/- buttons to entry size on every add (first call
        # may fire before layout settles, subsequent calls are instant no-ops).
        le.after_idle(self._sync_pair_btn_size)

        # Auto-scroll to reveal the new entry if it landed off-screen.
        if not self._loading:
            container = self._pairs_l_container
            def _scroll_to_new():
                parent = container.master
                if hasattr(parent, "_scroll_to_show_right"):
                    parent._scroll_to_show_right()
            le.after_idle(_scroll_to_new)
            self._sync_pairs_to_vars()

        return slot

    def _bind_pair_entry_nav(self, slot):
        """Arrow-key navigation for a pair (le / ve) entry slot.

        Left / Right: move the cursor normally within the entry; when the
        cursor is already at the edge (or the entry is empty), jump focus to
        the neighbouring pair entry in the same row.

        Up on le  : move cursor to the start (mirrors _bind_arrow_nav Up).
        Down on le : jump focus to the ve of the same pair.
        Up on ve   : jump focus to the le of the same pair.
        Down on ve : move cursor to the end (mirrors _bind_arrow_nav Down).
        """
        le = slot["le"]
        ve = slot["ve"]

        def _idx():
            try:
                return self._pair_entries.index(slot)
            except ValueError:
                return -1

        # ---- helpers -------------------------------------------------------
        def _move_cursor_left(w):
            pos = w.index("insert")
            if pos == 0:
                return False      # already at left edge
            w.icursor(pos - 1)
            if w.index("@0") > pos - 1:
                w.xview_scroll(-1, "units")
            return True

        def _move_cursor_right(w):
            pos = w.index("insert")
            end = w.index("end")
            if pos >= end:
                return False      # already at right edge
            w.icursor(pos + 1)
            width = w.winfo_width()
            if width > 1 and w.index(f"@{width - 1}") < pos + 1:
                w.xview_scroll(1, "units")
            return True

        # ---- le -------------------------------------------------------
        def _le_left(e):
            if not _move_cursor_left(le):
                i = _idx()
                if i > 0:
                    t = self._pair_entries[i - 1]["le"]
                    t.focus_set(); t.icursor("end")
            return "break"

        def _le_right(e):
            if not _move_cursor_right(le):
                i = _idx()
                if 0 <= i < len(self._pair_entries) - 1:
                    t = self._pair_entries[i + 1]["le"]
                    t.focus_set(); t.icursor(0); t.xview_moveto(0)
            return "break"

        def _le_up(e):
            le.icursor(0); le.xview_moveto(0)
            return "break"

        def _le_down(e):
            ve.focus_set()
            return "break"

        # ---- ve -------------------------------------------------------
        def _ve_left(e):
            if not _move_cursor_left(ve):
                i = _idx()
                if i > 0:
                    t = self._pair_entries[i - 1]["ve"]
                    t.focus_set(); t.icursor("end")
            return "break"

        def _ve_right(e):
            if not _move_cursor_right(ve):
                i = _idx()
                if 0 <= i < len(self._pair_entries) - 1:
                    t = self._pair_entries[i + 1]["ve"]
                    t.focus_set(); t.icursor(0); t.xview_moveto(0)
            return "break"

        def _ve_up(e):
            le.focus_set()
            return "break"

        def _ve_down(e):
            ve.icursor("end"); ve.xview_moveto(1)
            return "break"

        le.bind("<Left>",  _le_left)
        le.bind("<Right>", _le_right)
        le.bind("<Up>",    _le_up)
        le.bind("<Down>",  _le_down)
        ve.bind("<Left>",  _ve_left)
        ve.bind("<Right>", _ve_right)
        ve.bind("<Up>",    _ve_up)
        ve.bind("<Down>",  _ve_down)

    def _sync_pair_btn_size(self):
        """Resize the +/- wrapper frames to match the pixel width of an entry box.

        Uses the entry's pixel width for the wrapper width, but the button's own
        natural height — so the text stays vertically centred exactly as it was
        before the pixel-width wrapper was introduced.
        """
        if not self._pair_entries:
            return
        if self._pairs_add_wrap is None or self._pairs_rem_wrap is None:
            return
        le = self._pair_entries[0]["le"]
        lf = self._pair_entries[0]["lframe"]
        w = lf.winfo_reqwidth()    # entry width including any frame padding
        if w < 2:
            # Layout hasn't settled yet; retry after idle
            le.after_idle(self._sync_pair_btn_size)
            return
        # Use the button's own requested height so we never stretch it and
        # push the label text down.
        h_add = self._pairs_add_btn.winfo_reqheight()
        h_rem = self._pairs_rem_btn.winfo_reqheight()
        if h_add < 2 or h_rem < 2:
            le.after_idle(self._sync_pair_btn_size)
            return
        self._pairs_add_wrap.configure(width=w, height=h_add)
        self._pairs_rem_wrap.configure(width=w, height=h_rem)

    def _remove_last_pair(self):
        """Remove the rightmost pair entry from both rows."""
        if len(self._pair_entries) <= 1:
            return
        # Unpack ? buttons before destroying the last entry frame so pack order
        # stays clean, then repack them after the new last entry.
        l_qbtn, v_qbtn = (self._pairs_qbtns or [None, None])
        if l_qbtn:
            l_qbtn.pack_forget()
        if v_qbtn:
            v_qbtn.pack_forget()

        slot = self._pair_entries.pop()
        slot["lframe"].destroy()
        slot["vframe"].destroy()

        if l_qbtn:
            l_qbtn.pack(side="left", padx=(4, 0))
            l_qbtn.lift()
        if v_qbtn:
            v_qbtn.pack(side="left", padx=(4, 0))
            v_qbtn.lift()

        self._update_rem_btn()
        self._update_add_btn()
        self._sync_pairs_to_vars()

    def _update_rem_btn(self):
        """Enable the − button only when there are at least 2 pairs."""
        n = len(self._pair_entries)
        if self._pairs_rem_btn:
            self._pairs_rem_btn.configure(
                state="normal" if n > 1 else "disabled"
            )

    def _update_add_btn(self):
        """Enable the + button only when the last pair has both label and value filled."""
        if not self._pairs_add_btn:
            return
        # If globally disabled (reformat = no), leave it alone.
        if getattr(self, "_pairs_force_disabled", False):
            return
        if not self._pair_entries:
            self._pairs_add_btn.configure(state="normal")
            return
        last = self._pair_entries[-1]
        both_filled = bool(last["lvar"].get().strip()) and bool(last["vvar"].get().strip())
        self._pairs_add_btn.configure(state="normal" if both_filled else "disabled")

    def _sync_pairs_to_vars(self):
        """Write current pair entries back to label_elements / value_elements vars."""
        if self._loading:
            return
        lbls = [s["lvar"].get() for s in self._pair_entries]
        vals = [s["vvar"].get() for s in self._pair_entries]
        self._loading = True
        self._var("label_elements").set(" ".join(lbls))
        self._var("value_elements").set(" ".join(vals))
        self._loading = False
        self._mark_dirty()
        self._update_add_btn()

    def _rebuild_pairs_from_vars(self):
        """Clear and recreate pair entries from label_elements / value_elements vars."""
        if self._pairs_l_container is None:
            return
        for slot in self._pair_entries:
            slot["lframe"].destroy()
            slot["vframe"].destroy()
        self._pair_entries.clear()

        lraw = self._var("label_elements").get().strip()
        vraw = self._var("value_elements").get().strip()
        lparts = lraw.split() if lraw else []
        vparts = vraw.split() if vraw else []
        n = max(len(lparts), len(vparts), 1)
        for i in range(n):
            lv = lparts[i] if i < len(lparts) else ""
            vv = vparts[i] if i < len(vparts) else ""
            self._add_pair_slot(lv, vv)

    def _set_pairs_state(self, disabled):
        """Enable or disable all pair entries and the +/− buttons."""
        state = "disabled" if disabled else "normal"
        fg    = "gray"     if disabled else ""
        for lbl in self._pairs_lbl_widgets:
            lbl.configure(foreground=fg)
        for btn in self._pairs_qbtns:
            btn.configure(state=state)
        if self._pairs_add_btn:
            self._pairs_force_disabled = disabled
            if disabled:
                self._pairs_add_btn.configure(state="disabled")
            else:
                self._update_add_btn()
        if self._pairs_rem_btn:
            if disabled:
                self._pairs_rem_btn.configure(state="disabled")
            else:
                self._update_rem_btn()
        for slot in self._pair_entries:
            slot["le"].configure(state=state)
            slot["ve"].configure(state=state)

    def _build_reformat_tab(self):
        f = self._scrollable_tab("Reformat")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Reformat"); r += 1
        self._field(f, r, "Reformat",
                    lambda p: self._combo(p, "reformat", YES_NO),
                    tip_key="reformat"); r += 1
        self._build_pairs_rows(f, r, r + 1, r + 2); r += 3

        self._sep(f, r); r += 1
        self._section(f, r, "Labels"); r += 1
        self._field(f, r, "Sort",
                    lambda p: self._combo(p, "sort",
                    ["alphabet", "reverse", "unsorted"]),
                    tip_key="sort"); r += 1
        self._field(f, r, "Label Case",
                    lambda p: self._combo(p, "label_case",
                    ["default", "lower", "upper", "sentence"]),
                    tip_key="label_case"); r += 1
        self._field(f, r, "Zero Fill",
                    lambda p: self._combo(p, "zero_fill",
                    ["no", "adjacent", "snapshot"]),
                    tip_key="zero_fill"); r += 1
        self._field(f, r, "Fill First",
                    lambda p: self._combo(p, "fill_first", YES_NO),
                    tip_key="fill_first"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Label Merging"); r += 1
        self._field(f, r, "Label Strip Separators",
                    lambda p: self._combo(p, "label_strip_separators", YES_NO),
                    tip_key="label_strip_separators"); r += 1
        self._field(f, r, "Label Merge",
                    lambda p: self._combo(p, "label_merge", YES_NO),
                    tip_key="label_merge"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Merged Output"); r += 1
        self._field(f, r, "Merged Meta",
                    lambda p: self._combo(p, "merged_meta",
                    ["interleaved", "grouped"]),
                    tip_key="merged_meta"); r += 1

    def _apply_always_on_top(self, *_):
        on_top = self._var("always_on_top").get().strip().lower() == "yes"
        self.root.attributes("-topmost", on_top)

    def _build_advanced_tab(self):
        f = self._scrollable_tab("Advanced")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Window"); r += 1
        self._field(f, r, "Always on Top",
                    lambda p: self._combo(p, "always_on_top", YES_NO),
                    tip_key="always_on_top"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Fetch Mode"); r += 1
        self._field(f, r, "Headless Browser",
                    lambda p: self._combo(p, "headless_browser", YES_NO),
                    tip_key="headless_browser"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Retry & Performance"); r += 1
        self._field(f, r, "Min Gap",
                    lambda p: self._entry(p, "min_gap", 10),
                    tip_key="min_gap"); r += 1
        self._field(f, r, "Delay",
                    lambda p: self._entry(p, "delay", 10),
                    tip_key="delay"); r += 1
        self._field(f, r, "Retries",
                    lambda p: self._entry(p, "retries", 10),
                    tip_key="retries"); r += 1
        self._field(f, r, "End Passes",
                    lambda p: self._entry(p, "end_passes", 10),
                    tip_key="end_passes"); r += 1
        self._field(f, r, "Fallback Candidates",
                    lambda p: self._entry(p, "fallback_candidates", 10),
                    tip_key="fallback_candidates"); r += 1
        self._field(f, r, "Threads",
                    lambda p: self._entry(p, "threads", 10),
                    tip_key="threads"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Reset"); r += 1
        btn_frame = ttk.Frame(f)
        btn_frame.grid(row=r, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 6))
        ttk.Button(btn_frame, text="Reset to Last Saved",
                   command=self._reset_saved).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Reset to Defaults",
                   command=self._reset_defaults).pack(side="left")

    def _build_shortcuts_tab(self):
        f = self._scrollable_tab("Shortcuts")
        f.columnconfigure(1, weight=1)
        r = 0

        def _shortcut_row(parent, row, label, key, tip_key=""):
            """Non-tip rows: wide entry (cols 1-2) | Capture (col 3) | X (col 4).
            Tip row: entry (col 1) | frame(Capture ? X) (col 2)."""
            lbl = ttk.Label(parent, text=label)
            lbl.grid(row=row, column=0, sticky="w", padx=(10, 4), pady=3)

            has_tip = bool(tip_key and _TIPS.get(tip_key, ""))

            if has_tip:
                container = ttk.Frame(parent)
                container.grid(row=row, column=1, columnspan=4, sticky="ew",
                               padx=(0, 8), pady=3)
                clr_btn = ttk.Button(container, text="✕", width=2,
                                     command=lambda _v=self._var(key): _v.set(""))
                clr_btn.pack(side="right")
                _clr_tt = _Tooltip(clr_btn, _TIPS.get("shortcut_clear", ""))
                clr_btn.bind("<FocusIn>",
                             lambda e, b=clr_btn, tt=_clr_tt:
                                 tt._show(e) if self._in_canvas_viewport(b) else None,
                             add="+")
                clr_btn.bind("<FocusOut>", _clr_tt._hide, add="+")
                cap_btn = ttk.Button(container, text="Capture", width=8)
                cap_btn.configure(
                    command=lambda _v=self._var(key), _b=cap_btn:
                        _capture_shortcut(self.root, _v, _b))
                cap_btn.pack(side="right", padx=(0, 0))
                tip_btn = ttk.Button(container, text="?", width=2)
                tip_btn.pack(side="right", padx=(2, 2))
                _tip_tt = _Tooltip(tip_btn, _TIPS[tip_key])
                tip_btn.bind("<FocusIn>",
                             lambda e, b=tip_btn, tt=_tip_tt:
                                 tt._show(e) if self._in_canvas_viewport(b) else None,
                             add="+")
                tip_btn.bind("<FocusOut>", _tip_tt._hide, add="+")
                e = ttk.Entry(container, textvariable=self._var(key), width=22)
                self._bind_arrow_nav(e)
                self._bind_entry_undo(e, key)
                e.pack(side="left", fill="x", expand=True, padx=(0, 2))
            else:
                e = ttk.Entry(parent, textvariable=self._var(key), width=22)
                self._bind_arrow_nav(e)
                self._bind_entry_undo(e, key)
                e.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 4), pady=3)
                cap_btn = ttk.Button(parent, text="Capture", width=8)
                cap_btn.configure(
                    command=lambda _v=self._var(key), _b=cap_btn:
                        _capture_shortcut(self.root, _v, _b))
                cap_btn.grid(row=row, column=3, sticky="w", padx=(0, 2), pady=3)
                clr_btn = ttk.Button(parent, text="✕", width=2,
                                     command=lambda _v=self._var(key): _v.set(""))
                clr_btn.grid(row=row, column=4, sticky="w", padx=(0, 8), pady=3)
                _clr_tt2 = _Tooltip(clr_btn, _TIPS.get("shortcut_clear", ""))
                clr_btn.bind("<FocusIn>",
                             lambda e, b=clr_btn, tt=_clr_tt2:
                                 tt._show(e) if self._in_canvas_viewport(b) else None,
                             add="+")
                clr_btn.bind("<FocusOut>", _clr_tt2._hide, add="+")

            return e

        self._section(f, r, "Run"); r += 1
        _shortcut_row(f, r, "Start Run",   "shortcut_start"); r += 1
        _shortcut_row(f, r, "Stop Run",    "shortcut_stop");  r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Settings"); r += 1
        _shortcut_row(f, r, "Save Settings", "shortcut_save"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Navigation"); r += 1
        _shortcut_row(f, r, "Next Tab",     "shortcut_next_tab"); r += 1
        _shortcut_row(f, r, "Previous Tab", "shortcut_prev_tab"); r += 1

        tab_labels = ["URL", "Elements", "Schedule", "Output", "Reformat", "Advanced", "Shortcuts"]
        for i, tab_label in enumerate(tab_labels, start=1):
            key = f"shortcut_tab_{i}"
            _shortcut_row(f, r, f"Jump To Tab {i}  ({tab_label})", key); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Output"); r += 1
        _shortcut_row(f, r, "Focus Log", "shortcut_focus_log",
                      tip_key="shortcut_focus_log"); r += 1

    # -- Main UI assembly ------------------------------------------------------
    # -- Update checker --------------------------------------------------------
    def _check_for_updates(self):
        """Fetch the VERSION string from the raw file on GitHub and show a banner if newer."""
        if not GITHUB_REPO or not VERSION:
            return
        try:
            resp = requests.get(
                f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/wayback_element_tracker.py",
                timeout=10,
            )
            if resp.status_code != 200:
                return
            match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', resp.text, re.MULTILINE)
            if not match:
                return
            latest = match.group(1).lstrip("v")

            def parse(v):
                try:
                    return tuple(int(x) for x in v.split("."))
                except ValueError:
                    return (0,)

            if parse(latest) > parse(VERSION.lstrip("v")):
                repo_url = f"https://github.com/{GITHUB_REPO}"
                self.root.after(0, self._show_update_banner, repo_url)
        except Exception:
            pass   # silently ignore network / parse errors

    def _show_update_banner(self, url):
        """Insert a dismissible update banner above the notebook."""
        banner = tk.Frame(self.root, background="#fff3cd")
        banner.pack(fill="x", padx=6, pady=(0, 2), before=self.notebook)

        tk.Label(
            banner,
            text="ℹ️An update is available.",
            background="#fff3cd",
            font=("Segoe UI", 9),
            pady=5,
        ).pack(side="left", padx=(8, 4))

        link = tk.Label(
            banner,
            text="View on GitHub",
            background="#fff3cd",
            foreground="#0066cc",
            font=("Segoe UI", 9, "underline"),
            cursor="hand2",
            pady=5,
        )
        link.pack(side="left")
        link.bind("<Button-1>", lambda _: webbrowser.open(url))

        tk.Button(
            banner,
            text="×",
            relief="flat",
            background="#fff3cd",
            activebackground="#ffe89a",
            font=("Segoe UI", 9),
            cursor="hand2",
            bd=0,
            command=banner.destroy,
        ).pack(side="right", padx=(0, 6))
    def _build_ui(self):
        # Replace Tk's built-in Entry drag-select autoscroll proc.
        # The default scrolls 2 units AND sets the cursor to @$x (pixel pos under
        # the mouse).  On the right side x > widget_width, so @$x resolves to a
        # position several chars past where the view just scrolled, causing the
        # "teleport" effect.  On the left x < 0 clamps to 0, which is why that
        # side feels slower/more controlled by comparison.
        # This replacement scrolls exactly 1 character per 30 ms tick in either
        # direction, giving consistent, predictable behaviour on both sides.
        self.root.tk.eval(r"""
            proc ::tk::EntryAutoScan {w} {
                variable ::tk::Priv
                set x $Priv(x)
                if {![winfo exists $w]} return
                if {$x >= [winfo width $w]} {
                    $w xview scroll 1 units
                    set cur [$w index insert]
                    set end [$w index end]
                    if {$cur < $end} {
                        incr cur
                        $w icursor $cur
                        $w selection to $cur
                    }
                } elseif {$x < 0} {
                    $w xview scroll -1 units
                    set cur [$w index insert]
                    if {$cur > 0} {
                        incr cur -1
                        $w icursor $cur
                        $w selection to $cur
                    }
                } else {
                    return
                }
                set Priv(afterId) [after 30 [list ::tk::EntryAutoScan $w]]
            }
        """)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)
        # Intercept <<NotebookTabChanged>> at the widget level (fires before the Tcl
        # class-level TabChangedHandler) so we can suppress the automatic focus-move
        # to the first widget when switching tabs via keyboard shortcuts.
        self.notebook.bind("<<NotebookTabChanged>>",
                           lambda e: "break" if self._switching_tab_kb else None)

        self._build_url_tab()
        self._build_elements_tab()
        self._build_schedule_tab()
        self._build_output_tab()
        self._build_reformat_tab()
        self._build_advanced_tab()
        self._build_shortcuts_tab()

        # Button bar
        btn_bar = ttk.Frame(self.root)
        btn_bar.pack(fill="x", padx=6, pady=(0, 4))

        self._start_btn = ttk.Button(btn_bar, text="▶  Start", command=self._start)
        self._start_btn.pack(side="left", padx=(0, 4))

        self._stop_btn = ttk.Button(btn_bar, text="■  Stop", command=self._stop,
                                    state="disabled")
        self._stop_btn.pack(side="left", padx=(0, 4))

        # Right-side settings buttons in a sub-frame so pack order (left-to-right)
        # matches visual order and Tab cycles them in the correct direction.
        right_bar = ttk.Frame(btn_bar)
        right_bar.pack(side="right")
        ttk.Button(right_bar, text="Save Settings",
                   command=self._save).pack(side="left", padx=(0, 4))
        ttk.Button(right_bar, text="Save Settings As...",
                   command=self._save_as).pack(side="left", padx=(0, 4))
        ttk.Button(right_bar, text="Load Settings...",
                   command=self._load_from).pack(side="left")

        # Progress bar
        prog_frame = ttk.Frame(self.root)
        prog_frame.pack(fill="x", padx=6, pady=(0, 4))
        prog_frame.columnconfigure(0, weight=1)

        self._prog_canvas = tk.Canvas(prog_frame, height=20, bd=0,
                                      highlightthickness=0, cursor="arrow")
        self._prog_canvas.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._prog_canvas.bind("<Configure>", lambda e: self._draw_prog())

        self._progress_label = ttk.Label(prog_frame, text="", width=18, anchor="e")
        self._progress_label.grid(row=0, column=1, sticky="e")

        self._prog_frame = prog_frame

        # Output log
        log_frame = ttk.LabelFrame(self.root, text="Output log")
        self._log_frame = log_frame
        log_frame.pack(fill="both", expand=False, padx=6, pady=(0, 6))

        self._log_widget = scrolledtext.ScrolledText(
            log_frame, height=17, wrap="word",
            font=("Consolas", 9), state="disabled")
        self._log_widget.pack(fill="both", expand=True, padx=4, pady=4)

        self._bind_shortcuts()

    # -- Keyboard shortcuts ----------------------------------------------------
    def _bind_shortcuts(self):
        # Ctrl+Tab / Ctrl+Shift+Tab: cycle notebook tabs.
        # ttk.Notebook has these built in but they can be swallowed by focused
        # child widgets, so we re-bind them on the root window for reliability.
        # These are also handled by _rebind_shortcuts using the shortcut vars,
        # but we define the callbacks here so _rebind_shortcuts can reference them.

        # Enter on any button: invoke it (ttk buttons respond to Space but not
        # Return by default on Windows).
        self.root.bind_class("TButton", "<Return>",
                             lambda e: e.widget.invoke(), add="+")

        # Enter / Space on a combobox: open the dropdown (same as Alt+Down).
        # For editable combos, Space still types a space - only suppress on readonly.
        def _open_combo(e):
            w = e.widget
            if str(w.cget("state")) == "disabled":
                return
            w.event_generate("<Alt-Down>")
            if str(w.cget("state")) == "readonly":
                return "break"
        self.root.bind_class("TCombobox", "<Return>", _open_combo, add="+")
        self.root.bind_class("TCombobox", "<space>",  _open_combo, add="+")

        # Ctrl+Y / Ctrl+Shift+Z: redo inside element Text boxes.
        def _text_redo(e):
            try:
                e.widget.edit_redo()
            except tk.TclError:
                pass
            return "break"
        self.root.bind_class("Text", "<Control-y>",       _text_redo, add="+")
        self.root.bind_class("Text", "<Control-Shift-z>", _text_redo, add="+")

        # Apply the configurable shortcuts from settings vars
        self._rebind_shortcuts()

        # Elevate root-level shortcut bindings above widget class bindings.
        # By default, root.bind() fires at level 3 (widget → class → toplevel → all),
        # so any widget class that returns "break" for a sequence (e.g. tk.Text
        # consuming Ctrl+Tab as a tab-insertion) silently swallows the shortcut.
        # Prepending the root tag to each widget's bindtags makes root bindings
        # fire first, before the widget's own class handlers.
        def _elevate_root_bindings(widget):
            root_str = str(self.root)
            tags = widget.bindtags()
            if root_str not in tags:
                widget.bindtags((root_str,) + tags)
            for child in widget.winfo_children():
                _elevate_root_bindings(child)
        _elevate_root_bindings(self.root)

    def _rebind_shortcuts(self):
        """Unbind old configurable shortcuts and bind the current settings values."""

        def _bind_root(action, sequence, callback):
            """Bind *sequence* on root for *action*, replacing any previous binding."""
            old_seq, old_fid = self._bound_shortcuts.get(action, (None, None))
            # Unbind old sequence (only this handler, not all handlers on that sequence)
            if old_seq and old_fid:
                try:
                    self.root.unbind(old_seq, old_fid)
                except Exception:
                    pass
            if not sequence:
                self._bound_shortcuts[action] = (None, None)
                return
            fid = self.root.bind(sequence, callback, add="+")
            self._bound_shortcuts[action] = (sequence, fid)

        # -- Navigation callbacks ------------------------------------------
        def _next_tab(e):
            self._switching_tab_kb = True
            self.notebook.select((self.notebook.index("current") + 1) % len(self.notebook.tabs()))
            self.root.after_idle(lambda: setattr(self, "_switching_tab_kb", False))
            return "break"
        def _prev_tab(e):
            self._switching_tab_kb = True
            self.notebook.select((self.notebook.index("current") - 1) % len(self.notebook.tabs()))
            self.root.after_idle(lambda: setattr(self, "_switching_tab_kb", False))
            return "break"

        _bind_root("next_tab",   _shortcut_to_tk(self._var("shortcut_next_tab").get()),  _next_tab)
        _bind_root("prev_tab",   _shortcut_to_tk(self._var("shortcut_prev_tab").get()),  _prev_tab)
        _bind_root("save",       _shortcut_to_tk(self._var("shortcut_save").get()),      lambda e: self._save())
        _bind_root("start",      _shortcut_to_tk(self._var("shortcut_start").get()),
                   lambda e: self._start() if not self._running else None)
        _bind_root("stop",       _shortcut_to_tk(self._var("shortcut_stop").get()),
                   lambda e: self._stop() if self._running else None)
        _bind_root("focus_log",  _shortcut_to_tk(self._var("shortcut_focus_log").get()),
                   lambda e: (self._log_widget.configure(state="normal") or
                              self._log_widget.focus_set() or
                              self._log_widget.configure(state="disabled")))

        # Tab-jump shortcuts (Alt+1 through Alt+7)
        def _jump_tab(idx):
            if idx < len(self.notebook.tabs()):
                self._switching_tab_kb = True
                self.notebook.select(idx)
                self.root.after_idle(lambda: setattr(self, "_switching_tab_kb", False))

        for i in range(1, 8):
            key   = f"shortcut_tab_{i}"
            idx   = i - 1
            seq   = _shortcut_to_tk(self._var(key).get())
            _bind_root(f"tab_{i}", seq, lambda e, _idx=idx: _jump_tab(_idx))

    # -- Element level sync helpers -------------------------------------------
    def _sync_element_frames_from_vars(self):
        """Populate the level rows from the StringVars (called on load/reset)."""
        # Destroy all existing slot cards.
        for slot_id in list(self._active_slots):
            info = self._element_section_frames.pop(slot_id, None)
            if info:
                info["card"].destroy()
            for d in (self._element_levels, self._element_containers,
                      self._element_add_btns, self._element_add_spacers):
                d.pop(slot_id, None)
        self._active_slots.clear()
        self._next_slot = 1

        # Rebuild from consecutive element_N / extract_N vars.
        n = 1
        while f"element_{n}" in self._vars:
            val         = self._vars[f"element_{n}"].get()
            extract_var = self._vars.get(f"extract_{n}")
            extract     = extract_var.get() if extract_var else "text"
            parts       = self._split_element_levels(val)
            self._add_element_slot(parts=parts, extract=extract, _loading=True)
            n += 1

        # Always keep at least one slot.
        if not self._active_slots:
            self._add_element_slot(_loading=True)

        self._refresh_element_headers()

    def _sync_vars_from_element_frames(self):
        """Read the level entries back into StringVars (called before save/run).

        Slots are renumbered 1..N in their display order so the written settings
        file always has contiguous element_1 … element_N keys.
        """
        # Snapshot extract values by slot_id BEFORE clearing vars, because the
        # clearing loop zeroes the very StringVars the comboboxes are bound to.
        # Reading them after clearing would always yield "" instead of the
        # user's chosen value (elements are safe because _element_get_value
        # reads from the Text widget directly, not from a StringVar).
        extract_snapshot = {}
        for slot_id in self._active_slots:
            ev = self._vars.get(f"extract_{slot_id}")
            extract_snapshot[slot_id] = (ev.get() if ev else "") or "text"

        # Clear all existing element/extract vars so stale keys don't linger.
        _prev = self._loading
        self._loading = True
        for k in list(self._vars):
            if re.match(r'^(element|extract)_\d+$', k):
                self._vars[k].set("")
        # Write active slots in order, renumbered from 1.
        for new_num, slot_id in enumerate(self._active_slots, 1):
            self._vars[f"element_{new_num}"] = self._vars.get(
                f"element_{new_num}", tk.StringVar(value=""))
            self._var(f"element_{new_num}").set(self._element_get_value(slot_id))
            self._var(f"extract_{new_num}").set(
                extract_snapshot.get(slot_id) or "text")
        self._loading = _prev

    # -- Settings I/O ----------------------------------------------------------
    def _load_from_file(self):
        self._loading = True
        # Restore any real values that were hidden while fields were disabled,
        # so _update_states sees the true saved values when it runs below.
        for key, val in list(self._disabled_real_values.items()):
            self._var(key).set(val)
        self._disabled_real_values.clear()
        raw = _read_raw_settings()
        for k, v in raw.items():
            self._var(k).set(v)
        self._sync_element_frames_from_vars()
        self._rebuild_pairs_from_vars()
        self._split_date_composite("from")
        self._split_date_composite("to")
        self._loading = False
        self._unsaved = False
        self._update_states()
        self._save_snapshot()
        self._apply_always_on_top()

    def _save(self):
        self._sync_vars_from_element_frames()
        # For disabled fields the StringVar holds the visual default, not the
        # real value - use _disabled_real_values for those keys.
        raw = {k: self._disabled_real_values.get(k, sv.get())
               for k, sv in self._vars.items()}
        _write_settings(raw)
        self._unsaved = False
        self._save_snapshot()
        self._rebind_shortcuts()
        self._apply_always_on_top()

    def _reset_saved(self):
        if not tk.messagebox.askyesno(
                _DIALOGS["reset_saved"]["title"],
                _DIALOGS["reset_saved"]["message"]):
            return
        self._load_from_file()
        self._rebind_shortcuts()

    def _reset_defaults(self):
        if not tk.messagebox.askyesno(
                _DIALOGS["reset_defaults"]["title"],
                _DIALOGS["reset_defaults"]["message"]):
            return
        # Load only from DEFAULT_SETTINGS, ignoring settings.txt
        raw = {}
        for line in DEFAULT_SETTINGS.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            raw[k.strip().lower()] = v.strip()
        self._loading = True
        self._disabled_real_values.clear()
        for _dk in list(raw):
            if re.match(r'^element_\d+$', _dk):
                _dn = _dk[len("element_"):]
                raw.setdefault(f"extract_{_dn}", "text")
        for k, v in raw.items():
            self._var(k).set(v)
        self._sync_element_frames_from_vars()
        self._rebuild_pairs_from_vars()
        self._split_date_composite("from")
        self._split_date_composite("to")
        self._loading = False
        self._unsaved = False
        self._update_states()
        self._rebind_shortcuts()
        self._apply_always_on_top()

    def _save_as(self):
        """Save current settings to a user-chosen file."""
        path = filedialog.asksaveasfilename(
            title="Save Settings As",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="settings.txt",
        )
        if not path:
            return
        self._sync_vars_from_element_frames()
        raw = {k: self._disabled_real_values.get(k, sv.get())
               for k, sv in self._vars.items()}
        _write_settings(raw, path=path)

    def _load_from(self):
        """Load settings from a user-chosen file into the GUI (does not save to settings.txt)."""
        path = filedialog.askopenfilename(
            title="Load Settings",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return

        # Validate: the file must contain at least one recognised settings key.
        known_keys = set()
        for line in DEFAULT_SETTINGS.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, _ = line.partition("=")
            known_keys.add(k.strip().lower())

        found_keys = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, _ = line.partition("=")
                    k = k.strip().lower()
                    if k in known_keys or re.match(r'^(element|extract)_\d+$', k):
                        found_keys.add(k)
        except Exception as e:
            messagebox.showerror(
                _DIALOGS["load_error_read"]["title"],
                _DIALOGS["load_error_read"]["message"].format(e=e))
            return

        if not found_keys:
            messagebox.showerror(
                _DIALOGS["load_error_invalid"]["title"],
                _DIALOGS["load_error_invalid"]["message"],
            )
            return

        self._loading = True
        for key, val in list(self._disabled_real_values.items()):
            self._var(key).set(val)
        self._disabled_real_values.clear()
        raw = _read_raw_settings(path=path)
        for k, v in raw.items():
            self._var(k).set(v)
        self._sync_element_frames_from_vars()
        self._rebuild_pairs_from_vars()
        self._split_date_composite("from")
        self._split_date_composite("to")
        self._loading = False
        # Mark dirty only if the loaded file's values differ from what's currently
        # saved in settings.txt. Loading the same file should not trigger the
        # unsaved-changes warning on exit.
        saved_raw = _read_raw_settings(path=SETTINGS_PATH)
        self._unsaved = (raw != saved_raw)
        if not self._unsaved:
            self._save_snapshot()
        self._update_states()
        self._rebind_shortcuts()
        self._apply_always_on_top()

    def _on_close(self):
        if self._unsaved:
            # askyesnocancel: Yes=Save & exit, No=Exit without saving, Cancel=go back
            result = tk.messagebox.askyesnocancel(
                _DIALOGS["unsaved_changes"]["title"],
                _DIALOGS["unsaved_changes"]["message"])
            if result is None:   # Cancel - go back to the window
                return
            if result:           # Yes - save then exit
                self._save()
        self.root.destroy()

    # -- Run / Stop ------------------------------------------------------------
    def _validate(self):
        """Run pre-flight validation. Returns a list of human-readable error strings.

        An empty list means all checks passed and it is safe to save + run.
        Values for fields that are currently disabled in the UI are read from
        _disabled_real_values so we always validate the true setting, not the
        placeholder text shown while the field is greyed out.
        """
        errors = []

        def _real(key):
            return self._disabled_real_values.get(key, self._var(key).get()).strip()

        # 1. URL is required
        if not _real("url"):
            errors.append(_ERRORS["url_required"])

        # 2. At least one element must be filled in
        has_element = any(
            self._element_get_value(slot_id).strip()
            for slot_id in self._active_slots
        )
        if not has_element:
            errors.append(_ERRORS["element_required"])

        # 3. Numeric fields
        freq_all = self._var("frequency").get() == "all"
        headless = self._var("headless_browser").get() == "yes"

        # min_gap and fallback_candidates are disabled (and irrelevant) when
        # frequency = all, so only validate them in the other modes.
        if not freq_all:
            val = _real("min_gap")
            try:
                if float(val) < 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(_ERRORS["val_min_gap"].format(val=val))

            val = _real("fallback_candidates")
            try:
                if int(val) < 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(_ERRORS["val_fallback_candidates"].format(val=val))

        val = _real("end_passes")
        try:
            if int(val) < 0:
                raise ValueError
        except (ValueError, TypeError):
            errors.append(_ERRORS["val_end_passes"].format(val=val))

        val = _real("delay")
        try:
            float(val)
        except (ValueError, TypeError):
            errors.append(_ERRORS["val_delay"].format(val=val))

        val = _real("retries")
        try:
            if int(val) < 0:
                raise ValueError
        except (ValueError, TypeError):
            errors.append(_ERRORS["val_retries"].format(val=val))

        # threads is disabled (locked to 1) when headless_browser = yes.
        if not headless:
            val = _real("threads")
            try:
                if int(val) < 1:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(_ERRORS["val_threads"].format(val=val))

        # 4. Reformat pair validation (only when reformat = yes)
        if self._var("reformat").get() == "yes":
            rl = self._var("label_elements").get().strip()
            rv = self._var("value_elements").get().strip()
            if not rl or not rv:
                errors.append(_ERRORS["val_reformat_missing"])
            else:
                try:
                    label_slots = [int(x) for x in rl.split()]
                    value_slots = [int(x) for x in rv.split()]
                    if len(label_slots) != len(value_slots):
                        errors.append(
                            _ERRORS["val_reformat_mismatch"].format(
                                label_count=len(label_slots),
                                value_count=len(value_slots),
                            )
                        )
                    else:
                        for ls, vs in zip(label_slots, value_slots):
                            if ls == vs:
                                errors.append(
                                    _ERRORS["val_reformat_overlap"].format(slot=ls)
                                )
                                break
                except ValueError:
                    errors.append(_ERRORS["val_reformat_not_int"])

        return errors

    def _start(self):
        if self._running:
            return
        errors = self._validate()
        if errors:
            messagebox.showerror(
                _DIALOGS["cannot_start"]["title"],
                _DIALOGS["cannot_start"]["message"].format(errors="\n\n".join(errors)))
            return
        self._save()
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.configure(state="disabled")
        self._running = True
        self._proc = None
        self._start_btn.configure(state="disabled", text="Running...")
        self._stop_btn.configure(state="normal")
        self._prog_done   = 0
        self._prog_total  = 0
        self._prog_mode   = "wrap"
        self._prog_seen   = set()
        self._progress_label.configure(text="")
        self._wrap_start()

        flags = 0
        exe = sys.executable
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
            # pythonw.exe suppresses stdout even with a pipe; use python.exe instead
            if exe.lower().endswith("pythonw.exe"):
                exe = os.path.join(os.path.dirname(exe), "python.exe")
        try:
            core_path = os.path.join(
                os.path.dirname(os.path.abspath(sys.argv[0])),
                "wayback_element_tracker.py",
            )
            self._proc = subprocess.Popen(
                [exe, "-u", core_path, "--worker"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True, bufsize=1,
                creationflags=flags,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except Exception as e:
            self._append_log(f"[GUI] Failed to start worker: {e}\n")
            self._running = False
            self._on_done()
            return
        threading.Thread(target=self._read_proc, daemon=True).start()

    def _stop(self):
        if not self._running:
            return
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()
        self._running = False
        self._append_log("\n[GUI] Run stopped.\n")
        self._save_run_log()
        self._on_done()

    def _save_run_log(self):
        """Replicate save_log() using the text captured in the GUI log widget."""
        output = self._var("output").get().strip() or "wayback_results"
        stem = os.path.splitext(os.path.basename(output))[0]
        parent = os.path.dirname(output) or "."
        base_dir = os.path.join(parent, stem)
        log_path = os.path.join(base_dir, stem + ".log")

        current_run = self._log_widget.get("1.0", "end-1c").rstrip()
        if not current_run:
            return
        try:
            os.makedirs(base_dir, exist_ok=True)
            existing_runs = []
            if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
                with open(log_path, "r", encoding="utf-8") as f:
                    existing_runs = [r for r in f.read().split("\n\n") if r.strip()]
            all_runs = (existing_runs + [current_run])[-10:]
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(all_runs) + "\n")
            self._append_log(f"[Log]    Saved -> {os.path.abspath(log_path)}\n")
        except Exception as e:
            self._append_log(f"[Warning] Could not save log: {e}\n")

    def _read_proc(self):
        for line in self._proc.stdout:
            self._log_q.put(line)
        self._proc.wait()
        self._running = False
        self.root.after(0, self._on_done)

    def _on_done(self):
        self._start_btn.configure(state="normal", text="\u25b6  Start")
        self._stop_btn.configure(state="disabled")
        self._wrap_stop_anim()
        self._prog_mode = "idle"
        self._draw_prog()
        self._progress_label.configure(text="")

    # -- Canvas progress bar --------------------------------------------------
    # Two modes:
    #   "wrap" – a green segment slides left→right and wraps back (pre-results)
    #   "fill" – standard left-to-right proportional fill (during results)
    # Using a tk.Canvas gives us full control over both animations without
    # needing ctypes or platform-specific hacks.

    _PROG_TRACK_BG = "#ECECEC"   # bar trough background
    _PROG_TRACK_BD = "#BCBCBC"   # 1 px trough border
    _PROG_GREEN    = "#06B025"   # fill / segment colour
    _PROG_SEG_FRAC = 0.22        # wrap segment width as fraction of bar width
    _WRAP_SPEED_MS = 1100        # ms for the segment to cross the full width once
    _WRAP_FPS      = 60          # animation frame rate

    def _draw_prog(self):
        """Redraw the canvas bar in whatever mode is currently active."""
        c = self._prog_canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return
        c.delete("all")
        # Trough
        c.create_rectangle(0, 0, w - 1, h - 1,
                           fill=self._PROG_TRACK_BG, outline=self._PROG_TRACK_BD)
        if self._prog_mode == "wrap":
            self._draw_wrap_seg(w, h)
        elif self._prog_mode == "fill" and self._prog_total > 0:
            fill_w = max(0, int((w - 2) * self._prog_done / self._prog_total))
            if fill_w > 0:
                c.create_rectangle(1, 1, fill_w, h - 2,
                                   fill=self._PROG_GREEN, outline="")

    def _draw_wrap_seg(self, w, h):
        """Draw the segment, entering from the left and exiting to the right."""
        seg_w = max(50, int(w * self._PROG_SEG_FRAC))
        x     = int(self._wrap_pos)
        x1    = max(1, x)              # clamp: don't bleed past the left border
        x2    = min(x + seg_w, w - 1) # clamp: don't bleed past the right border
        if x2 > x1:
            self._prog_canvas.create_rectangle(x1, 1, x2, h - 2,
                                               fill=self._PROG_GREEN, outline="")

    def _wrap_start(self):
        """Start the animation with the segment fully off-screen to the left."""
        self.root.update_idletasks()
        w     = self._prog_canvas.winfo_width()
        seg_w = max(50, int(w * self._PROG_SEG_FRAC))
        self._wrap_pos = -float(seg_w)  # begin hidden behind the left wall
        self._wrap_tick()

    def _wrap_stop_anim(self):
        """Cancel the wrap tick without changing _prog_mode."""
        if self._wrap_job is not None:
            self.root.after_cancel(self._wrap_job)
            self._wrap_job = None

    def _wrap_tick(self):
        if self._prog_mode != "wrap":
            self._wrap_job = None
            return
        w = self._prog_canvas.winfo_width()
        if w > 1:
            seg_w            = max(50, int(w * self._PROG_SEG_FRAC))
            pixels_per_frame = w / (self._WRAP_SPEED_MS / (1000 / self._WRAP_FPS))
            self._wrap_pos  += pixels_per_frame
            # Reset only once the segment has fully left the right edge
            if self._wrap_pos >= w:
                self._wrap_pos = -float(seg_w)
            self._draw_prog()
        interval       = max(1, 1000 // self._WRAP_FPS)
        self._wrap_job = self.root.after(interval, self._wrap_tick)



    def _poll_log(self):
        MAX_LINES_PER_TICK = 200  # safety valve for extreme bursts

        # -- Pass 1: drain every queued item, split into progress signals vs log lines.
        # Progress signals are processed immediately regardless of queue depth so the
        # bar always reflects the latest state even during heavy output bursts.
        # Fatal messages emitted by sys.exit() in the worker.  sys.exit("msg")
        # writes to stderr, which is merged into stdout via stderr=STDOUT, so
        # these arrive as ordinary log lines – no special signal prefix.
        # We match them here so we can show a dialog without touching the tracker.
        _FATAL_PATTERNS = [
            # CDX API unreachable after all retries
            re.compile(r'\[CDX\]\s+Query failed after \d+ attempt'),
            # URL matched zero Wayback snapshots
            re.compile(r'^No snapshots to process\.$'),
        ]

        def _is_fatal_exit(line: str) -> bool:
            s = line.strip()
            return any(p.search(s) for p in _FATAL_PATTERNS)

        pending_log = []
        confirm_request = None  # (title, body) if a [_CONFIRM_] signal was received
        error_requests = []     # list of error message strings from [_ERROR_] signals
        try:
            while True:
                chunk = self._log_q.get_nowait()
                p = re.search(r'\[_PROG_ (\d+)/(\d+)\]', chunk)
                if p:
                    idx, total = int(p.group(1)), int(p.group(2))
                    if total > 0:
                        if self._prog_mode == "wrap":
                            self._wrap_stop_anim()
                            self._prog_mode  = "fill"
                            self._prog_total = total
                        self._prog_seen.add(idx)
                        self._prog_done = len(self._prog_seen)
                        pct = round(self._prog_done / total * 100)
                        self._draw_prog()
                        self._progress_label.configure(
                            text=f"{self._prog_done} / {total}  ({pct}%)")
                    # swallow – never add to the log widget
                else:
                    c = re.match(r'\[_CONFIRM_ (.+?)\|(.+)\]', chunk.strip())
                    if c:
                        confirm_request = (c.group(1).strip(), c.group(2).strip())
                        # swallow – dialog replaces this line in the GUI
                    elif chunk.strip().startswith('[_ERROR_ ') and chunk.strip().endswith(']'):
                        raw = chunk.strip()[len('[_ERROR_ '):-1]
                        error_msg = raw.replace('\\n', '\n').replace('\\\\', '\\')
                        error_requests.append(error_msg)
                        # swallow – already logged to the panel by _error_exit; dialog will show it
                    elif _is_fatal_exit(chunk):
                        error_requests.append(chunk.strip())
                        pending_log.append(chunk)   # keep in log as well
                    else:
                        pending_log.append(chunk)
        except _queue.Empty:
            pass

        # -- Pass 2: insert pending log text as a single batched operation.
        # Joining all chunks into one string and doing a single configure/insert/
        # configure call costs roughly the same as inserting one line, regardless
        # of how many lines arrived.  This mirrors how the Windows console coalesces
        # output before it hits the display layer, keeping the main thread free.
        # The cap guards against pathological bursts; overflow is re-queued for the
        # next tick so no lines are ever dropped.
        batch    = pending_log[:MAX_LINES_PER_TICK]
        overflow = pending_log[MAX_LINES_PER_TICK:]

        if batch:
            # Check scroll position BEFORE inserting – after insertion the
            # bottom fraction drops below 1.0 because new content extends
            # past the viewport, making a post-insert check always look like
            # the user has scrolled up even when they haven't.
            at_bottom = self._log_widget.yview()[1] >= 0.999
            self._log_widget.configure(state="normal")
            self._log_widget.insert("end", "".join(batch))
            if at_bottom:
                self._log_widget.see("end")
            self._log_widget.configure(state="disabled")

        for chunk in reversed(overflow):
            self._log_q.queue.appendleft(chunk)

        # -- Pass 3: show confirm dialog after log is flushed so the warning
        # text is visible before the dialog appears. Write the user's choice
        # to the worker's stdin so input() in the worker receives it.
        if confirm_request:
            title_txt, body_txt = confirm_request
            result = tk.messagebox.askyesno(title_txt, body_txt)
            try:
                self._proc.stdin.write("y\n" if result else "n\n")
                self._proc.stdin.flush()
            except Exception:
                pass

        # -- Pass 4: show error dialogs. The message is already in the log
        # (written by _error_exit before the signal); the dialog makes it
        # impossible to miss. Multiple errors are shown sequentially.
        for err_msg in error_requests:
            tk.messagebox.showerror(
                _DIALOGS["run_error"]["title"],
                _DIALOGS["run_error"]["message"].format(msg=err_msg))

        self.root.after(100, self._poll_log)

    def _append_log(self, msg):
        at_bottom = self._log_widget.yview()[1] >= 0.999
        self._log_widget.configure(state="normal")
        self._log_widget.insert("end", msg)
        if at_bottom:
            self._log_widget.see("end")
        self._log_widget.configure(state="disabled")

    def run(self):
        self.root.mainloop()



if __name__ == "__main__":
    WaybackGUI().run()

import sys
import os
import re
import threading
import requests
from wayback_element_tracker import DEFAULT_SETTINGS, MAX_ELEMENTS, VERSION, GITHUB_REPO

# -- GUI -----------------------------------------------------------------------
import tkinter as tk
import tkinter.font as _tkfont
from tkinter import ttk, scrolledtext, messagebox
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

# -- Tooltip texts (sourced from README) ---------------------------------------
_TIPS = {
    "url": (
        "The full URL of the page to track.\n"
        "e.g.  https://www.example.com"
    ),
    "filter_any": (
        "URL must match at least ONE filter, or will be skipped.\n\n"
        "(blank)      -> match only the exact URL, no variants\n"
        "*                -> include all URL variants\n"
        "/subpage  -> match URLs where /subpage appears at the end of the path\n"
        "key=value -> match only URLs containing key=value as a query parameter\n"
        "[filter]*      -> substring match anywhere in the URL\n"
        "                  e.g. key=* matches both key=1 and key=2\n"
        "                         /subpage* matches /subpage-a and /subpage-b\n"
        "![filter]      -> exclude instead of include\n"
        "                      (works with all of the above)\n\n"
        "e.g.  /images  key=value  !page=2"
    ),
    "filter_all": (
        "URL must match EVERY filter, or will be skipped.\n\n"
        "Same filter syntax as filter_any.\n\n"
        "Both fields can be used independently or together."
    ),
    "case_sensitive": (
        "Whether filter matching is case sensitive."
    ),
    "match_child_paths": (
        "Whether URL path filters (e.g. /subpage) also match pages deeper in the URL.\n\n"
        "yes -> /subpage also matches /subpage/child, /subpage/child/page, etc.\n"
        "no  -> /subpage matches only example.com/subpage exactly\n\n"
        "Note: substring filters like /subpage* always match child paths regardless of this setting."
    ),
    "element": (
        "The HTML element to track.\n"
        "Paste the HTML tag from Inspect Element.\n\n"
        "A parent element can be entered before the target element, separated by spaces:\n"
        "  <div class=\"row\"> <span class=\"value\">5%</span>\n\n"
        "To target a specific occurrence, place a number directly before the child element:\n"
        "  <div class=\"row\"> 2<span class=\"value\">text</span>\n"
        "   -> the 2nd span.value inside div.row\n\n"
        "Bare numbers after a parent select by child position:\n"
        "  <div class=\"row\"> 2 3\n"
        "   -> the 3rd child of the 2nd child of div.row\n\n"
        "All of these can be combined and stacked freely."
    ),
    "extract": (
        "What to extract from the element:\n\n"
        "text              -> visible text inside the element\n"
        "title              -> title=\"...\" attribute\n"
        "href              -> href=\"...\" attribute (links)\n"
        "src                -> src=\"...\" attribute (images, scripts)\n"
        "value            -> value=\"...\" attribute (inputs)\n"
        "content        -> content=\"...\" attribute (meta tags)\n"
        "alt                 -> alt=\"...\" attribute (image descriptions)\n"
        "placeholder -> placeholder=\"...\" attribute (input hints)\n"
        "datetime      -> datetime=\"...\" attribute (time elements)\n"
        "action           -> action=\"...\" attribute (forms)\n"
        "data-*           -> any custom data attribute\n"
        "                           e.g. data-count, data-value\n\n"
        "If a selector matches multiple elements on the page, all of\n"
        "them are captured as separately numbered columns or rows."
    ),
    "from_date": (
        "Start of the date range to search.\n"
        "Format: YYYYMMDD\n\n"
        "Leave blank to search all available snapshots."
    ),
    "to_date": (
        "End of the date range to search.\n"
        "Format: YYYYMMDD\n\n"
        "Leave blank to include up to the most recent snapshot."
    ),
    "frequency": (
        "Frequency of snapshots to check:\n\n"
        "all          -> every available snapshot\n"
        "hourly    -> one snapshot per hour\n"
        "daily      -> one snapshot per day\n"
        "weekly   -> one snapshot per week\n"
        "monthly -> one snapshot per month\n"
        "yearly     -> one snapshot per year"
    ),
    "sample_from": (
        "Which snapshot to pick within each frequency period:\n\n"
        "start     -> the snapshot closest to the START of the period\n"
        "middle -> the snapshot closest to the MIDDLE of the period\n"
        "end      -> the snapshot closest to the END of the period\n\n"
        "Has no effect when frequency = all."
    ),
    "collision_priority": (
        "When multiple URL variants have snapshots in the same time period, determines\n"
        "which one is preferred:\n\n"
        "time -> the variant whose timestamp is closest to the sample_from anchor wins\n"
        "filter -> earlier listed filter_any filters take priority over later ones\n\n"
        "Has no effect when no URL variants are tracked, or when split_output = files."
    ),
    "convention": (
        "us             -> month first  (November 5, 2023)\n"
        "european -> day first    (5 November 2023)"
    ),
    "date_style": (
        "long       -> November 5, 2023  /  5 November 2023\n"
        "short      -> Nov 5, 2023  /  5 Nov 2023\n"
        "numeric -> 11/5/2023  /  5/11/2023"
    ),
    "year_digits": (
        "4 -> 2023\n"
        "2 -> 23"
    ),
    "date_padding": (
        "yes -> 11/05/2023\n"
        "no  -> 11/5/2023"
    ),
    "time_format": (
        "12h -> 2:30 PM\n"
        "24h -> 14:30"
    ),
    "time_padding": (
        "yes -> 06:50\n"
        "no  -> 6:50"
    ),
    "show_seconds": "Show seconds in the time?",
    "show_month": (
        "Show the month in the output CSV?"
    ),
    "show_day": (
        "Show the day in the output CSV?"
    ),
    "show_year": (
        "Show the year in the output CSV?"
    ),
    "show_time": (
        "Show the time in the output CSV?"
    ),
    "output": (
        "CSV file name.\n\n"
        "After each run, a .log file is saved alongside the CSV with\n"
        "the same base name. Each new program execution is added to\n"
        "the end of the log file, which will track the last 10 runs."
    ),
    "file_override": (
        "Whether to overwrite the output file if it already exists.\n\n"
        "yes -> overwrite\n"
        "no  -> add an incrementing counter instead\n"
        "            e.g.  wayback_results.csv\n"
        "                    wayback_results_1.csv\n"
        "                    wayback_results_2.csv"
    ),
    "csv_layout": (
        "columns -> each attribute is a column, each snapshot is a row\n"
        "                    date | time | element | url | error\n\n"
        "rows       -> each attribute is a row, each snapshot is a column\n"
        "                    date | Jan 1 | Feb 1 | ...\n"
        "                    elem | value | value | ...\n\n"
        "The url column contains the full Wayback Machine URL of each snapshot.\n"
        "The error column is blank on success, or contains the failure reason\n"
        "(e.g. timeout, HTTP 404).\n\n"
        "When an element cannot be extracted, its cell is left empty.\n"
        "The console output distinguishes two cases:\n"
        "   (no element) -> element was not found anywhere on the page\n"
        "   (blank)           -> element was found but the extracted value was empty"
    ),
    "result_padding": (
        "Insert blank rows/columns for time periods\n"
        "with no archived snapshots.\n\n"
        "yes -> Jan 1 | Feb 1 | Mar 1 | ...\n"
        "           value |        | value | ...\n\n"
        "no  -> Jan 1 | Mar 1 | ...\n"
        "           value | value | ...\n\n"
        "Has no effect when frequency = all."
    ),
    "split_output": (
        "no          -> all variants written into one file; collisions resolved by collision_priority\n"
        "files       -> one output file per URL variant or filter\n"
        "merged -> one output file containing all filter groups separately"
    ),
    "reformat": (
        "Writes an additional [filename]_reformatted CSV per raw output file.\n\n"
        "Pairs two elements - one as a label and one as a value for that label.\n"
        "Moves each value element into one row (or column) per unique label.\n\n"
        "e.g.\n"
        "  date | Jan 1 | Feb 1 | ...        date  | Jan 1 | Feb 1 | ...\n"
        "  elem | label | label | ...  ->  label | value | value | ...\n"
        "  elem | value | value | ..."
    ),
    "label_elements": (
        "The index of the element(s) whose output become the LABELS in the reformatted file.\n\n"
        "e.g. 2 will treat element_2 as the label.\n\n"
        "Multiple indexes separated by spaces.\n"
        "The index of the label element is paired with the index of the value element at the\n"
        "same position."
    ),
    "value_elements": (
        "The index of the element(s) whose output become the VALUES in the reformatted file.\n\n"
        "e.g. 3 will treat element_3 as the value to track.\n\n"
        "Multiple indexes separated by spaces.\n"
        "For label_elements = 1 2 and value_elements = 3 4, elements 1 and 3 are paired and\n"
        "elements 2 and 4 are paired."
    ),
    "sort": (
        "How to order the label rows / columns in the reformatted file:\n\n"
        "unsorted -> labels appear in first-seen order\n"
        "alphabet -> alphabetical A\u2013Z (case insensitive)\n"
        "reverse    -> alphabetical Z\u2013A (case insensitive)"
    ),
    "zero_fill": (
        "When a label first appears partway through the timeline, places a 0 before its first value.\n\n"
        "no            -> disabled\n"
        "adjacent  -> places 0 in the cell directly before the first value\n"
        "snapshot -> places 0 in the snapshot before the first value\n"
        "               (only effective when result_padding is enabled)"
    ),
    "fill_first": (
        "Also place a 0 before labels whose first value appears at the very start of the timeline."
    ),
    "merged_meta": (
        "Controls where snapshot URLs and errors appear in the reformatted file when split_output = merged.\n"
        "Has no effect otherwise.\n\n"
        "grouped     -> all data rows for all groups appear first, then all url rows, then all error rows at the bottom\n"
        "interleaved -> each filter has a group label, then url, then error, then that filter's data rows"
    ),
    "headless_browser": (
        "Use a headless Chromium browser to fetch every snapshot instead of a plain HTTP request.\n\n"
        "Enable this when the regular fetch consistently returns blank or missing values that are visible\n"
        "when loading the page in a real browser.\n"
        "This executes each page's JavaScript fully before extracting elements, which is needed when\n"
        "a site populates element content with JavaScript.\n\n"
        "Note: significantly slower and resource intensive than the default method.\n"
        "Chromium (~300 MB) is downloaded automatically on first use."
    ),
    "min_gap": (
        "Minimum gap between 2 consecutive selected snapshots, as a fraction\n"
        "of the frequency period.\n"
        "Snapshots closer together than this are compared and the one farther\n"
        "from its anchor is discarded.\n\n"
        "0.5 -> half the period\n"
        "           e.g. ~15 days for monthly, 12 hours for daily\n"
        "0    -> disabled\n\n"
        "Has no effect when frequency = all."
    ),
    "delay": (
        "Seconds to wait between retry attempts and between CDX query retries."
    ),
    "retries": (
        "How many times to retry a failing snapshot or CDX query before giving up.\n\n"
        "Note: HTTP 404 and 403 responses are not retried, they fail immediately."
    ),
    "fallback_candidates": (
        "When a snapshot fails, how many closest snapshots from the same time period\n"
        "to try before giving up.\n\n"
        "Any snapshot farther than `min_gap` from the selected snapshot is excluded.\n"
        "Has no effect when frequency = all."
    ),
    "threads": (
        "Number of parallel threads for fetching snapshots.\n\n"
        "Has no effect when headless_browser = yes."
    ),
    "shortcut_focus_log": (
        "Focuses the output log panel so you can scroll through it or select\n"
        "and copy text without having to click it with the mouse."
    ),
}


def _read_raw_settings(path=SETTINGS_PATH):
    """Parse settings.txt into a flat dict, seeded from DEFAULT_SETTINGS."""
    raw = {}
    for line in DEFAULT_SETTINGS.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        raw[k.strip().lower()] = v.strip()
    for i in range(1, MAX_ELEMENTS + 1):
        if not raw.get(f"extract_{i}"):
            raw[f"extract_{i}"] = "text"

    if not os.path.exists(path):
        return raw

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip().lower()
            if k in raw:
                raw[k] = v.strip()
    return raw


def _write_settings(raw: dict, path=SETTINGS_PATH):
    """Re-write settings.txt from *raw*, preserving comments and section headers."""
    lines = []
    for line in DEFAULT_SETTINGS.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        k, _, _ = stripped.partition("=")
        k = k.strip().lower()
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
        self._element_text_widgets = {}   # key -> tk.Text for element_1..5
        self._running = False
        self._proc = None
        self._run_thread = None
        self._log_q = _queue.Queue()
        self._bound_shortcuts = {}   # action -> (sequence, func_id)

        # Parse defaults once for use by _set_state (show-default-when-disabled logic)
        self._defaults = {}
        for line in DEFAULT_SETTINGS.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            self._defaults[k.strip().lower()] = v.strip()
        for i in range(1, MAX_ELEMENTS + 1):
            self._defaults.setdefault(f"extract_{i}", "text")

        # Unsaved-changes tracking; _loading suppresses dirty-marking and
        # _update_states during bulk var changes (load / reset).
        self._loading = True
        self._unsaved = False
        self._disabled_real_values = {}   # key -> real value while field is disabled

        self._build_ui()
        self._load_from_file()   # sets _loading=False and calls _update_states()
        self._rebind_shortcuts() # re-register now that vars hold their loaded values
        for key in ("frequency", "headless_browser", "reformat", "split_output"):
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
                   for k, sv in self._vars.items()}
        for key, txt in self._element_text_widgets.items():
            current[key] = txt.get("1.0", "end-1c").replace("\n", " ").strip()
        if current == self._saved_raw:
            self._unsaved = False

    def _save_snapshot(self):
        """Capture the current true state for future dirty-checking."""
        self._saved_raw = {k: self._disabled_real_values.get(k, sv.get())
                           for k, sv in self._vars.items()}
        for key, txt in self._element_text_widgets.items():
            self._saved_raw[key] = txt.get("1.0", "end-1c").replace("\n", " ").strip()

    # -- Layout helpers --------------------------------------------------------
    def _scrollable_tab(self, title):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=title)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        cw = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        # scrollbar packed on demand below

        def _wheel(e):
            # Only scroll canvas if content is taller than the viewport
            if inner.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        def _combo_wheel(e):
            # Scroll the canvas, and block the combobox from cycling its value
            _wheel(e)
            return "break"

        def _bind_wheel(widget):
            if isinstance(widget, ttk.Combobox):
                widget.bind("<MouseWheel>", _combo_wheel, add="+")
            else:
                widget.bind("<MouseWheel>", _wheel, add="+")
            for child in widget.winfo_children():
                _bind_wheel(child)

        canvas.bind("<MouseWheel>", _wheel)

        def _on_inner_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            _bind_wheel(inner)
            # Show/hide scrollbar based on whether content overflows
            if inner.winfo_reqheight() > canvas.winfo_height():
                vsb.pack(side="right", fill="y")
            else:
                vsb.pack_forget()

        def _on_canvas_configure(e):
            canvas.itemconfig(cw, width=e.width)
            # Re-check scrollbar visibility when window is resized
            if inner.winfo_reqheight() > e.height:
                vsb.pack(side="right", fill="y")
            else:
                vsb.pack_forget()

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        return inner

    def _section(self, parent, row, text):
        ttk.Label(parent, text=text, font=("TkDefaultFont", 9, "bold")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(10, 2))

    def _sep(self, parent, row):
        ttk.Separator(parent, orient="horizontal").grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=8, pady=4)

    def _qbtn(self, parent, row, tip_key):
        """Place a ? button in column 3; attach tooltip from _TIPS. Returns button."""
        tip = _TIPS.get(tip_key, "")
        if not tip:
            return None
        btn = ttk.Button(parent, text="?", width=2)
        btn.grid(row=row, column=3, sticky="w", padx=(2, 8), pady=2)
        tt = _Tooltip(btn, tip)

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

        # Show/hide tooltip on keyboard focus, but only when the button is
        # actually visible (not scrolled out of view in the tab's canvas).
        btn.bind("<FocusIn>",  lambda e: tt._show(e) if _in_canvas_viewport(btn) else None, add="+")
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
        return e

    def _bind_entry_undo(self, entry, key):
        """Attach a debounced undo/redo stack to a ttk.Entry widget.
        Changes are committed to the undo stack ~400 ms after the last keystroke,
        so Ctrl+Z steps back in meaningful chunks rather than character by character.
        Loading/resetting settings clears the stack so stale history isn't replayed."""
        # Select-all on FocusIn is the default for both Entry and Combobox;
        # clear it on the next tick so tabbing in doesn't highlight the entire value.
        entry.bind("<FocusIn>",
                   lambda e: entry.after(0, entry.selection_clear), add="+")

        undo_stack = []
        redo_stack = []
        _timer   = [None]
        _baseline = [self._var(key).get()]   # last committed value

        def _commit():
            _timer[0] = None
            current = self._var(key).get()
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
                _baseline[0] = self._var(key).get()
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
                redo_stack.append(self._var(key).get())
                val = undo_stack.pop()
                _baseline[0] = val
                self._var(key).set(val)
                entry.icursor("end")
            return "break"

        def _redo(e):
            if redo_stack:
                undo_stack.append(self._var(key).get())
                val = redo_stack.pop()
                _baseline[0] = val
                self._var(key).set(val)
                entry.icursor("end")
            return "break"

        self._var(key).trace_add("write", _on_change)
        entry.bind("<Control-z>",       _undo)
        entry.bind("<Control-y>",       _redo)
        entry.bind("<Control-Shift-z>", _redo)

    def _bind_arrow_nav(self, widget):
        """Bind Up/Down arrows to move the cursor to the start/end of the field."""
        def _up(e):
            widget.icursor(0)
            return "break"
        def _down(e):
            widget.icursor("end")
            return "break"
        widget.bind("<Up>", _up)
        widget.bind("<Down>", _down)

    def _combo(self, parent, key, values, width=14, editable=False):
        cb = ttk.Combobox(parent, textvariable=self._var(key), values=values,
                          state="normal" if editable else "readonly", width=width)
        if editable:
            self._bind_entry_undo(cb, key)
        return cb

    # -- Tab builders ----------------------------------------------------------
    def _set_state(self, key, disabled, reason=""):
        row = self._field_rows.get(key)
        if not row:
            return
        w, lbl, btn = row["widget"], row["label"], row["qbtn"]
        # Values to display when a field is disabled (may differ from the default).
        _disabled_display = {"threads": "1"}

        # Update the hover tooltips on the widget and label.
        # When disabled: show the reason; when enabled: clear (tooltip won't appear).
        tip_text = f"Disabled because {reason}" if (disabled and reason) else ""
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
            self._var(key).set(_disabled_display.get(key, self._defaults.get(key, "")))
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

        self._set_state("sample_from",        freq_all,              "frequency = all")
        self._set_state("result_padding",      freq_all,              "frequency = all")
        self._set_state("min_gap",             freq_all,              "frequency = all")
        self._set_state("fallback_candidates", freq_all,              "frequency = all")
        self._set_state("threads",             headless,              "headless_browser = yes")
        self._set_state("label_elements",      no_reformat,           "reformat = no")
        self._set_state("value_elements",      no_reformat,           "reformat = no")
        self._set_state("sort",                no_reformat,           "reformat = no")
        self._set_state("zero_fill",           no_reformat,           "reformat = no")
        self._set_state("fill_first",          no_reformat,           "reformat = no")

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

    def _build_elements_tab(self):
        f = self._scrollable_tab("Elements")
        f.columnconfigure(1, weight=1)

        EXTRACT_OPTS = ["text", "title", "href", "src", "value", "content",
                        "alt", "placeholder", "datetime", "action", "data-"]
        EXTRACT_HEIGHT = len(EXTRACT_OPTS)
        r = 0
        for i in range(1, MAX_ELEMENTS + 1):
            if i > 1:
                self._sep(f, r); r += 1
            self._section(f, r, f"Element {i}"); r += 1

            ttk.Label(f, text="Element").grid(
                row=r, column=0, sticky="nw", padx=(10, 4), pady=3)

            # Multi-line Text widget: wraps long HTML, Enter adds a newline,
            # newlines are collapsed to spaces on save (settings.txt is line-based).
            # Match border color to the ttk theme (queried at runtime so it works
            # across themes). Falls back to a neutral gray if the theme uses
            # native rendering and doesn't expose a border color string.
            _border = ttk.Style().lookup("TEntry", "bordercolor") or "#999999"
            txt = tk.Text(f, width=48, height=3, wrap=tk.WORD,
                          font=("TkDefaultFont", 9),
                          relief="flat", borderwidth=0,
                          highlightthickness=1,
                          highlightbackground=_border,
                          highlightcolor=_border,
                          padx=3, pady=2,
                          undo=True)
            txt.grid(row=r, column=1, sticky="ew", padx=(0, 4), pady=3)
            self._element_text_widgets[f"element_{i}"] = txt

            # Tab / Shift-Tab move focus out of the text box instead of inserting whitespace.
            txt.bind("<Tab>",       lambda e, w=txt: (w.tk_focusNext().focus_set(), "break")[1])
            txt.bind("<Shift-Tab>", lambda e, w=txt: (w.tk_focusPrev().focus_set(), "break")[1])

            def _on_text_modified(e, _txt=txt):
                if _txt.edit_modified() and not self._loading:
                    self._unsaved = True
                _txt.edit_modified(False)
            txt.bind("<<Modified>>", _on_text_modified)
            self._qbtn(f, r, "element"); r += 1

            ttk.Label(f, text="Extract").grid(
                row=r, column=0, sticky="w", padx=(10, 4), pady=3)
            _ecb = ttk.Combobox(f, textvariable=self._var(f"extract_{i}"),
                         values=EXTRACT_OPTS, state="normal", width=18,
                         height=EXTRACT_HEIGHT)
            _ecb.grid(row=r, column=1, sticky="w", padx=(0, 4), pady=3)
            _ecb.bind("<<ComboboxSelected>>",
                      lambda e, cb=_ecb: cb.selection_clear(), add="+")
            self._bind_entry_undo(_ecb, f"extract_{i}")
            self._qbtn(f, r, "extract"); r += 1

    def _build_schedule_tab(self):
        f = self._scrollable_tab("Schedule")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Date Range"); r += 1
        self._field(f, r, "From Date",
                    lambda p: self._entry(p, "from_date", 12),
                    tip_key="from_date"); r += 1
        self._field(f, r, "To Date",
                    lambda p: self._entry(p, "to_date", 12),
                    tip_key="to_date"); r += 1

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

    def _build_reformat_tab(self):
        f = self._scrollable_tab("Reformat")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Reformat"); r += 1
        self._field(f, r, "Reformat",
                    lambda p: self._combo(p, "reformat", YES_NO),
                    tip_key="reformat"); r += 1
        self._field(f, r, "Label Elements",
                    lambda p: self._entry(p, "label_elements", 20),
                    tip_key="label_elements"); r += 1
        self._field(f, r, "Value Elements",
                    lambda p: self._entry(p, "value_elements", 20),
                    tip_key="value_elements"); r += 1
        self._field(f, r, "Sort",
                    lambda p: self._combo(p, "sort",
                    ["alphabet", "reverse", "unsorted"]),
                    tip_key="sort"); r += 1
        self._field(f, r, "Zero Fill",
                    lambda p: self._combo(p, "zero_fill",
                    ["no", "adjacent", "snapshot"]),
                    tip_key="zero_fill"); r += 1
        self._field(f, r, "Fill First",
                    lambda p: self._combo(p, "fill_first", YES_NO),
                    tip_key="fill_first"); r += 1
        self._field(f, r, "Merged Meta",
                    lambda p: self._combo(p, "merged_meta",
                    ["interleaved", "grouped"]),
                    tip_key="merged_meta"); r += 1

    def _build_advanced_tab(self):
        f = self._scrollable_tab("Advanced")
        f.columnconfigure(1, weight=1)
        r = 0

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
        self._field(f, r, "Fallback Candidates",
                    lambda p: self._entry(p, "fallback_candidates", 10),
                    tip_key="fallback_candidates"); r += 1
        self._field(f, r, "Threads",
                    lambda p: self._entry(p, "threads", 10),
                    tip_key="threads"); r += 1

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
                _Tooltip(clr_btn, "Clear this shortcut")
                cap_btn = ttk.Button(container, text="Capture", width=8)
                cap_btn.configure(
                    command=lambda _v=self._var(key), _b=cap_btn:
                        _capture_shortcut(self.root, _v, _b))
                cap_btn.pack(side="right", padx=(0, 0))
                tip_btn = ttk.Button(container, text="?", width=2)
                tip_btn.pack(side="right", padx=(2, 2))
                _Tooltip(tip_btn, _TIPS[tip_key])
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
                _Tooltip(clr_btn, "Clear this shortcut")

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
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)

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
        self._stop_btn.pack(side="left")

        # Right-side settings buttons in a sub-frame so pack order (left-to-right)
        # matches visual order and Tab cycles them in the correct direction.
        right_bar = ttk.Frame(btn_bar)
        right_bar.pack(side="right")
        ttk.Button(right_bar, text="Save Settings",
                   command=self._save).pack(side="left", padx=(0, 4))
        ttk.Button(right_bar, text="Reset to Last Saved",
                   command=self._reset_saved).pack(side="left", padx=(0, 4))
        ttk.Button(right_bar, text="Reset to Defaults",
                   command=self._reset_defaults).pack(side="left")

        # Output log
        log_frame = ttk.LabelFrame(self.root, text="Output log")
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

        # Ctrl+Z / Ctrl+Y: undo and redo inside element text boxes.
        def _redo(e):
            try:
                e.widget.edit_redo()
            except tk.TclError:
                pass
            return "break"
        self.root.bind_class("Text", "<Control-y>",       _redo, add="+")
        self.root.bind_class("Text", "<Control-Shift-z>", _redo, add="+")

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
            tabs = self.notebook.tabs()
            self.notebook.select((self.notebook.index("current") + 1) % len(tabs))
            return "break"
        def _prev_tab(e):
            tabs = self.notebook.tabs()
            self.notebook.select((self.notebook.index("current") - 1) % len(tabs))
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
        for i in range(1, 8):
            key   = f"shortcut_tab_{i}"
            idx   = i - 1
            seq   = _shortcut_to_tk(self._var(key).get())
            _bind_root(f"tab_{i}", seq,
                       lambda e, _idx=idx: self.notebook.select(_idx)
                       if _idx < len(self.notebook.tabs()) else None)

    # -- Element text-widget sync helpers -------------------------------------
    def _sync_text_widgets_from_vars(self):
        """Push StringVar values into the element tk.Text widgets (on load/reset)."""
        for key, txt in self._element_text_widgets.items():
            txt.delete("1.0", "end")
            txt.insert("1.0", self._var(key).get())
            # Reset the modified flag synchronously so the <<Modified>> event
            # that Tkinter queues for the delete/insert above is ignored by
            # _on_text_modified when the event loop eventually processes it.
            txt.edit_modified(False)

    def _sync_vars_from_text_widgets(self):
        """Read element tk.Text widgets back into StringVars (before save/run).
        Newlines are collapsed to a single space so settings.txt stays line-based."""
        for key, txt in self._element_text_widgets.items():
            val = txt.get("1.0", "end-1c").replace("\n", " ").strip()
            self._var(key).set(val)

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
        self._sync_text_widgets_from_vars()
        self._loading = False
        self._unsaved = False
        self._update_states()
        self._save_snapshot()

    def _save(self):
        self._sync_vars_from_text_widgets()
        # For disabled fields the StringVar holds the visual default, not the
        # real value - use _disabled_real_values for those keys.
        raw = {k: self._disabled_real_values.get(k, sv.get())
               for k, sv in self._vars.items()}
        _write_settings(raw)
        self._unsaved = False
        self._save_snapshot()
        self._rebind_shortcuts()

    def _reset_saved(self):
        if not tk.messagebox.askyesno(
                "Reset to Last Saved",
                "Discard all unsaved changes and reload from settings.txt?"):
            return
        self._load_from_file()
        self._rebind_shortcuts()

    def _reset_defaults(self):
        if not tk.messagebox.askyesno(
                "Reset to Defaults",
                "This will clear all settings back to their default values.\n"
                "Settings.txt will not be changed until you click Save Settings.\n\n"
                "Continue?"):
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
        for i in range(1, MAX_ELEMENTS + 1):
            raw.setdefault(f"extract_{i}", "text")
        for k, v in raw.items():
            self._var(k).set(v)
        self._sync_text_widgets_from_vars()
        self._loading = False
        self._unsaved = False
        self._update_states()
        self._rebind_shortcuts()

    def _on_close(self):
        if self._unsaved:
            # askyesnocancel: Yes=Save & exit, No=Exit without saving, Cancel=go back
            result = tk.messagebox.askyesnocancel(
                "Unsaved Changes",
                "You have unsaved changes.\nSave before closing?")
            if result is None:   # Cancel - go back to the window
                return
            if result:           # Yes - save then exit
                self._save()
        self.root.destroy()

    # -- Run / Stop ------------------------------------------------------------
    def _start(self):
        if self._running:
            return
        self._save()
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.configure(state="disabled")
        self._running = True
        self._proc = None
        self._start_btn.configure(state="disabled", text="Running...")
        self._stop_btn.configure(state="normal")

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

    # -- Log polling -----------------------------------------------------------
    def _poll_log(self):
        try:
            while True:
                chunk = self._log_q.get_nowait()
                self._log_widget.configure(state="normal")
                self._log_widget.insert("end", chunk)
                self._log_widget.see("end")
                self._log_widget.configure(state="disabled")
        except _queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _append_log(self, msg):
        self._log_widget.configure(state="normal")
        self._log_widget.insert("end", msg)
        self._log_widget.see("end")
        self._log_widget.configure(state="disabled")

    def run(self):
        self.root.mainloop()



if __name__ == "__main__":
    WaybackGUI().run()

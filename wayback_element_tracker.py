import atexit
import csv
import re
import sys
import time
import os
import calendar
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
from urllib.parse import urlparse, parse_qs, unquote
import requests
from bs4 import BeautifulSoup
from wayback_dialogs import _DIALOGS, _ERRORS

# Playwright is optional - imported lazily when headless_browser = yes
_playwright_available = None  # None = not yet checked

# -- Constants ----------------------------------------------------------------
VERSION = "v2.5.0"
GITHUB_REPO = "Matteo-MDG/Wayback-Element-Tracker"

CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"

FREQ_MAP = {
    "all": None,
    "hourly": "%Y%m%d%H",
    "daily": "%Y%m%d",
    "weekly": "%Y%W",
    "monthly": "%Y%m",
    "yearly": "%Y",
}

FREQ_SECONDS = {
    "all": 0,
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,
    "yearly": 31536000,
}

KNOWN_ATTRS = [
    "title", "href", "src", "value", "content",
    "alt", "placeholder", "datetime", "action",
]

# True when launched by the GUI (which passes --worker). Used to gate
# output that is only meaningful to the GUI (e.g. [_PROG_] progress ticks).
_GUI_MODE = "--worker" in sys.argv


# -- URL Filter Helpers --------------------------------------------------------
def parse_filters(val: str) -> tuple:
    """
    Parse the filter setting (a space-separated list of tokens) into
    (cdx_wildcard, filters).

      cdx_wildcard : bool  – whether to append '*' to the URL in the CDX query
      filters      : list  – list of filter dicts, each with keys:
                               pattern : str | None
                               mode    : 'all' | 'path' | 'path_prefix' |
                                         'contains' | 'exact'
                               negate  : bool  (True when prefixed with '!')

    Token syntax:
      (blank)      – exact URL only, no variants fetched, no post-filtering
      *            – all variants (include-all)
      /subpage     – path segment-boundary match; the pattern must appear as a
                     complete path segment (followed by '/' or end of path)
      /subpage*    – path prefix match; matches any URL whose path contains
                     the pattern as a prefix or substring
      key=value    – exact query-string parameter match
      key=value*   – substring match anywhere in the URL
      !<token>     – same as above but *excludes* matching URLs instead

    Multiple tokens are separated by spaces, e.g.:
      sort=usage !sort=usage_rate
      * !page=2 !page=3
    """
    v = val.strip()
    if not v:
        return False, []          # exact URL, no variants, no post-filter

    filters = []
    cdx_wildcard = False

    for token in v.split():
        token = token.strip()
        if not token:
            continue
        negate = token.startswith("!")
        t = token[1:].strip() if negate else token

        if t == "*":
            cdx_wildcard = True
            filters.append({"pattern": None, "mode": "all", "negate": negate})
        elif t.startswith("/"):
            cdx_wildcard = True
            if t.endswith("*"):
                # /subpage* → path prefix match (strip the *)
                filters.append({"pattern": t[:-1].rstrip("/"), "mode": "path_prefix", "negate": negate})
            else:
                # /subpage or /subpage/ → segment-boundary match
                # Normalise trailing slash so both forms behave identically
                filters.append({"pattern": t.rstrip("/") or "/", "mode": "path", "negate": negate})
        elif t.endswith("*"):
            cdx_wildcard = True
            filters.append({"pattern": t[:-1], "mode": "contains", "negate": negate})
        elif t:
            cdx_wildcard = True
            filters.append({"pattern": t, "mode": "exact", "negate": negate})

    return cdx_wildcard, filters


def _single_filter_matches(url: str, pattern: str | None, mode: str,
                           case_sensitive: bool = True,
                           match_child_paths: bool = True) -> bool:
    """Return True if url matches one filter rule (ignoring negate)."""
    if mode == "all":
        return True
    if not case_sensitive and pattern is not None:
        url = url.lower()
        pattern = pattern.lower()
    if mode == "path":
        # Segment-boundary match: pattern must appear as a complete path segment.
        # With match_child_paths=True:  also matches deeper children (/a matches /a/b/c)
        # With match_child_paths=False: only matches the exact end of the path
        path = urlparse(url).path
        idx = path.find(pattern)
        if idx == -1:
            return False
        end = idx + len(pattern)
        if match_child_paths:
            return end == len(path) or path[end] == "/"
        else:
            return end == len(path)
    if mode == "path_prefix":
        # Prefix/substring match within the path only.
        return pattern in urlparse(url).path
    if mode == "contains":
        return pattern in url
    # mode == "exact": pattern must be one of the individual query-string parameters.
    # The Wayback CDX stores URLs with & encoded in several ways:
    #   &amp%3B  (HTML entity &amp; with ; percent-encoded)
    #   \u0026   (JSON-style unicode escape, stored literally)
    # Normalise all variants to plain & before parsing.
    query = urlparse(url).query
    query = html.unescape(unquote(query))   # &amp%3B -> &amp; -> &
    query = query.replace('\\u0026', '&')   # literal \u0026 -> &
    params = parse_qs(query, keep_blank_values=True)
    if "=" in pattern:
        key, _, val = pattern.partition("=")
        return key in params and val in params[key]
    else:
        return pattern in params


def url_matches_filters(url: str, filters_any: list, filters_all: list,
                        case_sensitive: bool = True,
                        match_child_paths: bool = True) -> bool:
    """
    Return True if *url* passes both filter fields.

    filter_any rules  (OR logic):
      - Exclude (!): URL must not match any of them.
      - Include:     URL must match at least one (if any exist).

    filter_all rules  (AND logic):
      - Exclude (!): URL must not match all of them simultaneously.
      - Include:     URL must match every one of them (if any exist).

    Both fields must be satisfied for the URL to pass.
    """
    cs = case_sensitive
    isp = match_child_paths

    # -- filter_any (OR) -------------------------------------------------------
    any_includes = [f for f in filters_any if not f["negate"]]
    any_excludes = [f for f in filters_any if f["negate"]]

    for f in any_excludes:
        if _single_filter_matches(url, f["pattern"], f["mode"], cs, isp):
            return False

    if any_includes:
        if not any(_single_filter_matches(url, f["pattern"], f["mode"], cs, isp)
                   for f in any_includes):
            return False

    # -- filter_all (AND) ------------------------------------------------------
    all_includes = [f for f in filters_all if not f["negate"]]
    all_excludes = [f for f in filters_all if f["negate"]]

    if all_excludes:
        if all(_single_filter_matches(url, f["pattern"], f["mode"], cs, isp)
               for f in all_excludes):
            return False

    if all_includes:
        if not all(_single_filter_matches(url, f["pattern"], f["mode"], cs, isp)
                   for f in all_includes):
            return False

    return True

# -- Logging & Sequential Print Buffer ----------------------------------------
_log_lines = []
_print_lock = threading.Lock()
_print_buffer = {}  # index -> message string
_next_to_print = [1]  # list so threads share the same mutable object


def log(msg: str=""):
    """Print immediately and record in log."""
    with _print_lock:
        print(msg, flush=True)
        _log_lines.append(msg)


def buffer_and_flush(index: int, msg: str, total: int = 0):
    """
    Buffer a snapshot result by its 1-based index, then flush all
    consecutive buffered messages so output appears in order.
    Immediately emits a [_PROG_ index/total] signal line first so the GUI
    progress bar updates the instant any thread finishes, regardless of order.
    """
    with _print_lock:
        # Progress tick: printed immediately, not held by the sequential buffer,
        # not saved to _log_lines. The GUI filters this prefix out of the display.
        if total and _GUI_MODE:
            print(f"[_PROG_ {index}/{total}]", flush=True)
        _print_buffer[index] = msg
        while _next_to_print[0] in _print_buffer:
            m = _print_buffer.pop(_next_to_print[0])
            print(m, flush=True)
            _log_lines.append(m)
            _next_to_print[0] += 1


def drain_buffer():
    """Flush all remaining consecutive buffer entries.
    Call this after a threaded run_pass to ensure all output is printed
    before any subsequent log messages.
    """
    with _print_lock:
        while _next_to_print[0] in _print_buffer:
            m = _print_buffer.pop(_next_to_print[0])
            print(m, flush=True)
            _log_lines.append(m)
            _next_to_print[0] += 1


def _confirm(title: str, short_msg: str) -> bool:
    """
    Prompt the user Y/N to continue.
    In GUI mode, emits a [_CONFIRM_] signal so the GUI can show a dialog;
    the user's button press is piped back via stdin, and no visible prompt
    is printed (the dialog replaces it).
    In CLI mode, prints the prompt directly and reads from stdin.
    Returns True if the user confirms.
    """
    if _GUI_MODE:
        encoded_title = title.replace("\\", "\\\\").replace("\n", "\\n")
        encoded_msg   = short_msg.replace("\\", "\\\\").replace("\n", "\\n")
        print(f"[_CONFIRM_ {encoded_title}|{encoded_msg}]", flush=True)
        try:
            answer = input("").strip().lower()
        except EOFError:
            answer = "n"
    else:
        try:
            answer = input("  Continue anyway? [Y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
    return answer in ("y", "yes")


def _error_exit(msg: str):
    """
    Exit with an error message. In CLI mode behaves like sys.exit(msg),
    printing to stderr. In GUI mode, logs the message to the output panel
    and emits a [_ERROR_] signal so the GUI can show a messagebox, then
    exits cleanly with code 1 to avoid a duplicate stderr print.
    """
    if _GUI_MODE:
        log(msg)
        encoded = msg.replace("\\", "\\\\").replace("\n", "\\n")
        print(f"[_ERROR_ {encoded}]", flush=True)
        sys.exit(1)
    else:
        sys.exit(msg)


def save_log(output_path: str, log_dir: str = None):
    stem = os.path.splitext(os.path.basename(output_path))[0]
    if log_dir:
        log_path = os.path.join(log_dir, stem + ".log")
    else:
        log_path = os.path.splitext(output_path)[0] + ".log"
    MAX_RUNS = 10
    try:
        # Read existing runs from the log file (runs are separated by blank lines)
        existing_runs = []
        if os.path.exists(log_path) and os.path.getsize(log_path) > 0:
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Split on blank lines; filter out any empty strings from splitting
            existing_runs = [r for r in content.split("\n\n") if r.strip()]

        # Add the current run and keep only the last MAX_RUNS runs
        current_run = "\n".join(_log_lines)
        all_runs = existing_runs + [current_run]
        all_runs = all_runs[-MAX_RUNS:]

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(all_runs))
            f.write("\n")
        print(f"[Log]    Saved -> {os.path.abspath(log_path)}")
    except Exception as e:
        clean_err = re.sub(r'https?://\S+', '', str(e)).strip()
        print(f"[Warning] Could not save log: {clean_err}")


# -- Attribute Helpers ---------------------------------------------------------
def get_extractable_attrs(element) -> list:
    """Return all extractable attribute names found on this element."""
    found = []
    for attr in KNOWN_ATTRS:
        if element.get(attr) is not None:
            found.append(attr)
    for attr in element.attrs:
        if attr.startswith("data-"):
            found.append(attr)
    return found


def extract_value(element, extract: str) -> str:
    if extract == "text":
        return element.get_text(separator=" ", strip=True)
    val = element.get(extract)
    if val is None:
        return ""
    return " ".join(val).strip() if isinstance(val, list) else str(val).strip()


# -- HTML Element Parser -------------------------------------------------------
def parse_element_html(raw: str, slot: int) -> tuple:
    """
    Parse a raw HTML snippet into (selector_chain, extractable_attrs).

    selector_chain is a list of step dicts: {"sel": str|None, "nth": int|None}
      sel=None  -> bare nth-child step: pick the Nth direct child element
      nth=None  -> all matches of sel
      nth=N     -> only the Nth match of sel (1-based)

    Supported formats:

    Single element:
        element_1 = <img class="hero-img" alt="Example Title" src="...">

    Parent > child chain (space between closing and opening tag):
        element_1 = <div class="data-row"> <span class="value">5%</span>
        element_1 = <table class="stats"> <tbody> <tr> <td class="pct">

    Nth match of a child selector (integer directly before child tag):
        element_1 = <div class="paragraph1"> 2<span class="paragraph2">text</span>
        -> the 2nd span.paragraph2 inside div.paragraph1

    Nth direct child(ren) - bare integers after a tag (no following tag):
        element_1 = <div class="paragraph1"> 2 3
        -> the 3rd child element of the 2nd child element of div.paragraph1
    """
    raw = raw.strip()

    # Find all opening HTML tags (exclude closing tags via negative lookahead for /)
    opening_tag_re = re.compile(r'<(?!/)[^>]+>')
    tag_matches = list(opening_tag_re.finditer(raw))

    if not tag_matches:
        _error_exit(_ERRORS["parse_element"].format(label=f"element_{slot}", slot=slot))

    def tag_to_selector(tag_html, step_num):
        """Convert an opening-tag string to a CSS selector string + extractable attrs."""
        soup = BeautifulSoup(tag_html, "lxml")
        element = None
        for tag in soup.find_all(True):
            if tag.name not in ("html", "body"):
                element = tag
                break
        if element is None:
            label = (f"element_{slot}" if step_num == 0
                     else f"element_{slot} (step {step_num + 1})")
            _error_exit(_ERRORS["parse_element"].format(label=label, slot=slot))
        classes = element.get("class", [])
        sel = element.name
        if classes:
            sel += "." + ".".join(classes)
        elem_id = element.get("id", "").strip()
        if elem_id:
            sel += "#" + elem_id
        return sel, get_extractable_attrs(element)

    steps = []
    extractables = []

    # First tag - never has a preceding nth number
    first_sel, first_attrs = tag_to_selector(tag_matches[0].group(), 0)
    steps.append({"sel": first_sel, "nth": None})
    extractables = first_attrs

    # Subsequent opening tags - inspect the gap before each for an nth number
    for i in range(1, len(tag_matches)):
        gap = raw[tag_matches[i - 1].end(): tag_matches[i].start()]
        gap_text = re.sub(r'<[^>]+>', '', gap)      # strip any closing tags in gap
        nums = re.findall(r'\b(\d+)\b', gap_text)
        nth = int(nums[-1]) if nums else None

        sel, attrs = tag_to_selector(tag_matches[i].group(), i)
        steps.append({"sel": sel, "nth": nth})
        extractables = attrs

    # Suffix after the last opening tag - bare integers become nth-child steps,
    # but ONLY when there are no closing tags in the suffix. A closing tag means
    # the user pasted element content (e.g. ">39% </div>"), not navigation indices.
    suffix = raw[tag_matches[-1].end():]
    has_closing_tag = bool(re.search(r'</\s*\w', suffix))
    if not has_closing_tag:
        suffix_text = re.sub(r'<[^>]+>', '', suffix)
        for n in re.findall(r'\b(\d+)\b', suffix_text):
            steps.append({"sel": None, "nth": int(n)})
            extractables = []                        # no known attrs for bare steps

    return steps, extractables


# -- Date / Time Formatting ----------------------------------------------------
def format_date(dt: datetime, cfg: dict) -> str:
    show_month = cfg["show_month"]
    show_day = cfg["show_day"]
    show_year = cfg["show_year"]
    convention = cfg["convention"]
    style = cfg["date_style"]
    pad = cfg["date_padding"]
    year_dig = cfg["year_digits"]

    day_str = f"{dt.day:02d}" if pad else str(dt.day)
    month_str = f"{dt.month:02d}" if pad else str(dt.month)
    month_long = dt.strftime("%B")
    month_abbr = dt.strftime("%b")
    year = dt.strftime("%Y") if year_dig == 4 else dt.strftime("%y")

    if style in ("long", "short"):
        month_word = month_long if style == "long" else month_abbr
        if convention == "us":
            parts = []
            if show_month: parts.append(month_word)
            if show_day: parts.append(str(dt.day) + ",")
            if show_year: parts.append(year)
            return " ".join(parts).rstrip(",").strip()
        else:
            parts = []
            if show_day: parts.append(str(dt.day))
            if show_month: parts.append(month_word)
            if show_year: parts.append(year)
            return " ".join(parts).strip()
    else:
        if convention == "us":
            components = []
            if show_month: components.append(month_str)
            if show_day: components.append(day_str)
            if show_year: components.append(year)
        else:
            components = []
            if show_day: components.append(day_str)
            if show_month: components.append(month_str)
            if show_year: components.append(year)
        return "/".join(components)


def format_time(dt: datetime, cfg: dict) -> str:
    fmt = cfg["time_format"]
    seconds = cfg["show_seconds"]
    time_pad = cfg["time_padding"]
    minute = dt.strftime("%M")
    second = dt.strftime("%S")
    if fmt == "12h":
        hour = dt.strftime("%I").lstrip("0") or "12"
        period = dt.strftime("%p")
        return f"{hour}:{minute}:{second} {period}" if seconds else f"{hour}:{minute} {period}"
    else:
        hour = dt.strftime("%H") if time_pad else str(dt.hour)
        return f"{hour}:{minute}:{second}" if seconds else f"{hour}:{minute}"


def format_datetime(dt: datetime, cfg: dict) -> tuple:
    show_any_date = cfg["show_month"] or cfg["show_day"] or cfg["show_year"]
    date_str = format_date(dt, cfg).strip() if show_any_date else ""
    time_str = format_time(dt, cfg) if cfg["show_time"] else ""
    return date_str, time_str


def ts_to_dt(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S")


# -- Anchor Point Calculation --------------------------------------------------
def anchor_dt_for(dt: datetime, frequency: str, anchor: str) -> datetime:
    if anchor == "middle":
        start = anchor_dt_for(dt, frequency, "start")
        end   = anchor_dt_for(dt, frequency, "end")
        return start + (end - start) / 2
    if frequency == "yearly":
        return datetime(dt.year, 1, 1) if anchor == "start" \
            else datetime(dt.year, 12, 31, 23, 59, 59)
    if frequency == "monthly":
        if anchor == "start":
            return datetime(dt.year, dt.month, 1)
        last = calendar.monthrange(dt.year, dt.month)[1]
        return datetime(dt.year, dt.month, last, 23, 59, 59)
    if frequency == "weekly":
        week_start = dt - timedelta(days=dt.weekday())
        return week_start.replace(hour=0, minute=0, second=0) if anchor == "start" \
            else (week_start + timedelta(days=6)).replace(hour=23, minute=59, second=59)
    if frequency == "daily":
        return dt.replace(hour=0, minute=0, second=0) if anchor == "start" \
            else dt.replace(hour=23, minute=59, second=59)
    if frequency == "hourly":
        return dt.replace(minute=0, second=0) if anchor == "start" \
            else dt.replace(minute=59, second=59)
    return dt


def iter_periods(from_dt: datetime, to_dt: datetime, frequency: str):
    """
    Yield the start datetime of every period bucket from from_dt's bucket
    through to_dt's bucket (inclusive), advancing by one period each step.
    Used by result_padding to enumerate gaps.
    """
    if frequency == "yearly":
        current = datetime(from_dt.year, 1, 1)
        while current <= to_dt:
            yield current
            current = datetime(current.year + 1, 1, 1)
    elif frequency == "monthly":
        current = datetime(from_dt.year, from_dt.month, 1)
        while current <= to_dt:
            yield current
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1)
            else:
                current = datetime(current.year, current.month + 1, 1)
    elif frequency == "weekly":
        week_start = from_dt - timedelta(days=from_dt.weekday())
        current = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        while current <= to_dt:
            yield current
            current += timedelta(weeks=1)
    elif frequency == "daily":
        current = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while current <= to_dt:
            yield current
            current += timedelta(days=1)
    elif frequency == "hourly":
        current = from_dt.replace(minute=0, second=0, microsecond=0)
        while current <= to_dt:
            yield current
            current += timedelta(hours=1)


def prev_period_dt(dt: datetime, frequency: str) -> datetime:
    """Return the start of the period immediately preceding dt's period."""
    if frequency == "yearly":
        return datetime(dt.year - 1, 1, 1)
    if frequency == "monthly":
        return datetime(dt.year - 1, 12, 1) if dt.month == 1 \
               else datetime(dt.year, dt.month - 1, 1)
    if frequency == "weekly":
        week_start = (dt - timedelta(days=dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return week_start - timedelta(weeks=1)
    if frequency == "daily":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    if frequency == "hourly":
        return dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return dt


def yesno(val: str) -> bool:
    return val.strip().lower() == "yes"


DEFAULT_SETTINGS = """\
# --- URL -------------------------------------------------------------------------------------------------
url = 
filter_any = 
filter_all = 
case_sensitive = yes
match_child_paths = no

# --- HTML ELEMENTS ---------------------------------------------------------------------------------------
element_1 = 
extract_1 = 

# --- DATE RANGE ------------------------------------------------------------------------------------------
from_date = 
to_date = 

# --- SNAPSHOT FREQUENCY ----------------------------------------------------------------------------------
frequency = all
sample_from = start
collision_priority = time

# --- DATE & TIME FORMAT ----------------------------------------------------------------------------------
convention = us
date_style = long
year_digits = 4
date_padding = no
time_format = 12h
time_padding = yes
show_seconds = no

# --- OUTPUT ----------------------------------------------------------------------------------------------
output = wayback_results
file_override = yes
csv_layout = rows
result_padding = no
split_output = no
show_month = yes
show_day = yes
show_year = yes
show_time = yes

# --- REFORMAT --------------------------------------------------------------------------------------------
reformat = no
label_elements = 
value_elements = 
sort = alphabet
zero_fill = no
fill_first = no
merged_meta = interleaved
label_merge = no
label_strip_separators = no
label_case = default

# --- FETCH MODE ------------------------------------------------------------------------------------------
headless_browser = no

# --- ADVANCED --------------------------------------------------------------------------------------------
min_gap = 0.5
delay = 10
retries = 5
end_passes = 2
fallback_candidates = 1
threads = 3

# --- WINDOW ------------------------------------------------------------------------------------------
always_on_top = no

# --- KEYBOARD SHORTCUTS ----------------------------------------------------------------------------------
shortcut_save = Ctrl+S
shortcut_start = F5
shortcut_stop = Escape
shortcut_focus_log = Alt+L
shortcut_next_tab = Ctrl+Tab
shortcut_prev_tab = Ctrl+Shift+Tab
shortcut_tab_1 = Alt+1
shortcut_tab_2 = Alt+2
shortcut_tab_3 = Alt+3
shortcut_tab_4 = Alt+4
shortcut_tab_5 = Alt+5
shortcut_tab_6 = Alt+6
shortcut_tab_7 = Alt+7
"""


def load_settings(path="settings.txt") -> dict:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_SETTINGS)
        abs_path = os.path.abspath(path)
        print(f"[Setup]  settings.txt not found -- created a blank one at: {abs_path}")
        print(f"[Setup]  Fill in your url and element_1, then run the program again.")
        sys.exit(0)

    # Seed raw defaults by parsing DEFAULT_SETTINGS - single source of truth.
    raw = {}
    for line in DEFAULT_SETTINGS.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        raw[k.strip().lower()] = v.strip()
    # extract_N defaults to "text" when blank — seed for every element_N present
    for _k in list(raw):
        if re.match(r'^element_\d+$', _k):
            _n = _k[len("element_"):]
            if not raw.get(f"extract_{_n}"):
                raw[f"extract_{_n}"] = "text"

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower().replace("-", "_")

            value = value.strip()
            if value:
                if key in raw or re.match(r'^(element|extract)_\d+$', key):
                    raw[key] = value

    # Seed extract defaults for any element_N loaded from the file
    for _k in list(raw):
        if re.match(r'^element_\d+$', _k):
            _n = _k[len("element_"):]
            if not raw.get(f"extract_{_n}"):
                raw[f"extract_{_n}"] = "text"

    if not raw["url"]:
        _error_exit(_ERRORS["url_missing"])

    # Strip trailing * from URL - treat it the same as filter_any = *
    url = raw["url"]
    if url.endswith("*"):
        url = url.rstrip("*")
        if not raw["filter_any"].strip():
            raw["filter_any"] = "*"
        raw["_url_wildcard"] = "yes"
    else:
        raw["_url_wildcard"] = "no"

    # Auto-append .csv if the output setting has no file extension
    if not os.path.splitext(raw["output"])[1]:
        raw["output"] = raw["output"] + ".csv"

    if raw["frequency"] not in FREQ_MAP:
        _error_exit(_ERRORS["frequency_invalid"].format(values=', '.join(FREQ_MAP)))
    if raw["sample_from"].lower() not in ("start", "middle", "end"):
        _error_exit(_ERRORS["sample_from_invalid"])
    if raw["csv_layout"].lower() not in ("columns", "rows"):
        _error_exit(_ERRORS["csv_layout_invalid"])
    if raw["year_digits"] not in ("2", "4"):
        _error_exit(_ERRORS["year_digits_invalid"])
    for field in ("from_date", "to_date"):
        val = raw[field]
        if val and (not val.isdigit() or len(val) != 8):
            _error_exit(_ERRORS["date_format_invalid"].format(field=field, val=val))

    try:
        min_gap_frac = float(raw["min_gap"])
        if min_gap_frac < 0:
            raise ValueError
    except ValueError:
        _error_exit(_ERRORS["min_gap_invalid"])

    try:
        fallback_candidates = int(raw["fallback_candidates"])
        if fallback_candidates < 0:
            raise ValueError
    except ValueError:
        _error_exit(_ERRORS["fallback_candidates_invalid"])

    try:
        end_passes = int(raw["end_passes"])
        if end_passes < 0:
            raise ValueError
    except ValueError:
        _error_exit(_ERRORS["end_passes_invalid"])

    min_gap_secs = int(FREQ_SECONDS.get(raw["frequency"], 0) * min_gap_frac)

    cdx_any, filters_any = parse_filters(raw["filter_any"])
    cdx_all, filters_all = parse_filters(raw["filter_all"])
    cdx_wildcard = cdx_any or cdx_all or (raw["_url_wildcard"] == "yes")

    # -- Reformat validation ---------------------------------------------------
    do_reformat = yesno(raw["reformat"])
    reformat_pairs = []
    sort = raw["sort"].strip().lower()

    if do_reformat:
        if sort not in ("alphabet", "reverse", "unsorted"):
            _error_exit(_ERRORS["sort_invalid"])
        rl = raw["label_elements"].strip()
        rv = raw["value_elements"].strip()
        if not rl or not rv:
            _error_exit(_ERRORS["reformat_missing"])
        try:
            label_slots = [int(x) for x in rl.split()]
            value_slots = [int(x) for x in rv.split()]
        except ValueError:
            _error_exit(_ERRORS["reformat_not_int"])
        if len(label_slots) != len(value_slots):
            _error_exit(
                _ERRORS["reformat_mismatch"].format(
                    label_count=len(label_slots), value_count=len(value_slots)
                )
            )
        for ls, vs in zip(label_slots, value_slots):
            if ls == vs:
                _error_exit(_ERRORS["reformat_overlap"].format(slot=ls))
        reformat_pairs = list(zip(label_slots, value_slots))

    elements = []
    # Find all element_N slots, sorted by N, regardless of upper bound
    slot_nums = sorted(
        int(m.group(1))
        for k in raw
        if (m := re.match(r'^element_(\d+)$', k))
    )
    for i in slot_nums:
        html = raw.get(f"element_{i}", "")
        if not html:
            continue
        extract = raw.get(f"extract_{i}", "text").lower()
        if extract != "text" and not extract.startswith("data-") \
                and extract not in KNOWN_ATTRS:
            _error_exit(_ERRORS["extract_invalid"].format(slot=i, extract=extract))
        selector_chain, extractables = parse_element_html(html, i)

        def _step_display(step):
            if step["sel"] is None:
                return f"[child {step['nth']}]"
            elif step["nth"] is not None:
                return f"{step['sel']} [{step['nth']}]"
            else:
                return step["sel"]

        selector = " > ".join(_step_display(s) for s in selector_chain)
        last_step_is_bare_child = selector_chain[-1]["sel"] is None
        if extract != "text" and extract not in extractables and not last_step_is_bare_child:
            others = [a for a in extractables if a != extract]
            msg = (
                f"[Warning] extract_{i} = '{extract}' not found on pasted element '{selector}'.\n"
                f"          It may still exist in live snapshots."
            )
            if others:
                msg += f"\n          Other available: {', '.join(others)}"
            print(msg)
        elements.append({"slot": i, "selector_chain": selector_chain, "selector": selector, "extract": extract})

    if not elements:
        _error_exit(_ERRORS["element_missing"])

    try:
        threads = int(raw["threads"])
        if threads < 1:
            raise ValueError
    except ValueError:
        _error_exit(_ERRORS["threads_invalid"])

    cfg = {
        "url": url,
        "elements": elements,
        "from_date": raw["from_date"],
        "to_date": raw["to_date"],
        "frequency": raw["frequency"],
        "sample_from": raw["sample_from"].lower(),
        "convention": raw["convention"].lower(),
        "date_style": raw["date_style"].lower(),
        "year_digits": int(raw["year_digits"]),
        "date_padding": yesno(raw["date_padding"]),
        "time_format": raw["time_format"].lower(),
        "time_padding": yesno(raw["time_padding"]),
        "show_seconds": yesno(raw["show_seconds"]),
        "show_month": yesno(raw["show_month"]),
        "show_day": yesno(raw["show_day"]),
        "show_year": yesno(raw["show_year"]),
        "show_time": yesno(raw["show_time"]),
        "csv_layout": raw["csv_layout"].lower(),
        "result_padding": yesno(raw["result_padding"]),
        "filter_any_raw": raw["filter_any"],
        "filter_all_raw": raw["filter_all"],
        "filter_cdx_wildcard": cdx_wildcard,
        "filters_any": filters_any,
        "filters_all": filters_all,
        "case_sensitive": yesno(raw["case_sensitive"]),
        "min_gap_secs": min_gap_secs,
        "min_gap_frac": float(raw["min_gap"]),
        "output": raw["output"],
        "file_override": yesno(raw["file_override"]),
        "delay": float(raw["delay"]),
        "retries": int(raw["retries"]),
        "end_passes": end_passes,
        "fallback_candidates": fallback_candidates,
        "threads": threads,
        "headless_browser": yesno(raw["headless_browser"]),
        "reformat": do_reformat,
        "reformat_pairs": reformat_pairs,
        "sort": sort,
        "zero_fill": raw["zero_fill"].strip().lower(),
        "fill_first": yesno(raw["fill_first"]),
        "label_merge": yesno(raw.get("label_merge", "no")),
        "label_strip_separators": yesno(raw.get("label_strip_separators", "yes" if yesno(raw.get("label_merge", "no")) else "no")),
        "label_case": raw.get("label_case", "default").strip().lower(),
        "split_output": raw["split_output"].strip().lower(),
        "merged_meta": raw["merged_meta"].strip().lower(),
        "match_child_paths": yesno(raw["match_child_paths"]),
        "collision_priority": "",  # set after validation below
    }

    if cfg["zero_fill"] not in ("no", "adjacent", "snapshot"):
        _error_exit(_ERRORS["zero_fill_invalid"])

    if cfg["label_case"] not in ("default", "lower", "upper", "sentence"):
        _error_exit(_ERRORS["label_case_invalid"])

    if cfg["split_output"] not in ("no", "files", "merged"):
        _error_exit(_ERRORS["split_output_invalid"])

    if cfg["merged_meta"] not in ("interleaved", "grouped"):
        _error_exit(_ERRORS["merged_meta_invalid"])

    # -- collision_priority validation -------------------------------------------
    collision_priority = raw["collision_priority"].strip().lower()
    if collision_priority not in ("time", "filter"):
        _error_exit(_ERRORS["collision_priority_invalid"])
    cfg["collision_priority"] = collision_priority

    return cfg


# -- Step 1: Preflight checks --------------------------------------------------
PREFLIGHT_URL_THRESHOLD      = 20   # distinct URLs before warning
PREFLIGHT_SNAPSHOT_THRESHOLD = 500  # estimated snapshots before warning
PREFLIGHT_TIMEOUT            = 10   # seconds per preflight request

def _cdx_preflight(cfg: dict):
    """
    Run two cheap CDX queries in parallel before the main fetch to warn the
    user early if the query is unexpectedly large, before any hanging can occur.

    1. Distinct-URL check: fetches only unique original URLs (collapse=urlkey)
       up to PREFLIGHT_URL_THRESHOLD + 1. If the limit is hit the user is
       warned that the query matches at least that many distinct URLs.
       Only runs when filter_cdx_wildcard is True, since an exact URL can only
       ever match one distinct URL.

    2. Snapshot-count check: uses showNumPages=true to get a page count and
       estimates total snapshots. If the estimate exceeds
       PREFLIGHT_SNAPSHOT_THRESHOLD the user is warned before the main fetch.
       Always runs, regardless of whether a CDX wildcard is used, because even
       a single URL can accumulate an extremely large number of snapshots.

    Both checks run in parallel with a short timeout. A timeout itself is
    treated as a warning signal: if the archive cannot answer a cheap preflight
    query within the timeout window, the main query is almost certainly
    excessively large. Results are presented URL check first, then count check.
    Either refusal exits immediately.
    """
    cdx_url = cfg["url"] + ("*" if cfg["filter_cdx_wildcard"] else "")
    base_params = {"url": cdx_url, "filter": "statuscode:200"}
    if cfg["from_date"]:
        base_params["from"] = cfg["from_date"]
    if cfg["to_date"]:
        base_params["to"] = cfg["to_date"]

    def _check_urls():
        params = {
            **base_params,
            "fl":       "original",
            "collapse": "urlkey",
            "output":   "json",
            "limit":    PREFLIGHT_URL_THRESHOLD + 1,
        }
        resp = requests.get(CDX_API, params=params, timeout=PREFLIGHT_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
        return max(0, len(rows) - 1) if rows else 0

    def _check_count():
        params = {
            **base_params,
            "collapse":     "digest",
            "showNumPages": "true",
        }
        resp = requests.get(CDX_API, params=params, timeout=PREFLIGHT_TIMEOUT)
        resp.raise_for_status()
        CDX_PAGE_SIZE = 100_000
        return int(resp.text.strip() or 0) * CDX_PAGE_SIZE

    # Run checks in parallel. URL check is only meaningful for wildcard queries.
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_urls  = ex.submit(_check_urls) if cfg["filter_cdx_wildcard"] else None
        f_count = ex.submit(_check_count)

    # -- 1. Distinct-URL result (wildcard queries only) ------------------------
    if f_urls is not None:
        try:
            n_distinct = f_urls.result()
            if n_distinct > PREFLIGHT_URL_THRESHOLD:
                log(
                    f"[Warning] This query matches at least {n_distinct} distinct URLs.\n"
                    f"          This may be unintentionally broad (e.g. tracking every\n"
                    f"          sub-ID under a path like /page/1, /page/2, ...)."
                )
                if not _confirm(
                    _DIALOGS["preflight_many_urls"]["title"],
                    _DIALOGS["preflight_many_urls"]["message"].format(n=n_distinct),
                ):
                    sys.exit(_ERRORS["preflight_aborted_urls"])
                log("")
        except requests.Timeout:
            log(
                "[Warning] The preflight URL check timed out.\n"
                "          This likely means the query matches an extremely large\n"
                "          number of distinct URLs."
            )
            if not _confirm(
                _DIALOGS["preflight_timeout_urls"]["title"],
                _DIALOGS["preflight_timeout_urls"]["message"],
            ):
                sys.exit(_ERRORS["preflight_aborted_timeout_urls"])
            log("")
        except Exception as e:
            clean_err = re.sub(r'https?://\S+', '', str(e)).strip().strip(":")
            log(f"[CDX]    Preflight URL check failed ({clean_err}) -- skipping.")

    # -- 2. Snapshot-count result (always runs) --------------------------------
    try:
        estimate = f_count.result()
        if estimate > PREFLIGHT_SNAPSHOT_THRESHOLD:
            log(
                f"[Warning] This query will fetch roughly {estimate:,} snapshots.\n"
                f"          This may take a long time and place heavy load on the archive.\n"
                f"          Make sure this is intended before continuing."
            )
            if not _confirm(
                _DIALOGS["preflight_high_count"]["title"],
                _DIALOGS["preflight_high_count"]["message"].format(estimate=f"{estimate:,}"),
            ):
                sys.exit(_ERRORS["preflight_aborted_count"])
            log("")
    except requests.Timeout:
        log(
            "[Warning] The preflight snapshot count check timed out.\n"
            "          This likely means the query will fetch an extremely large\n"
            "          number of snapshots."
        )
        if not _confirm(
            _DIALOGS["preflight_timeout_count"]["title"],
            _DIALOGS["preflight_timeout_count"]["message"],
        ):
            sys.exit(_ERRORS["preflight_aborted_timeout_count"])
        log("")
    except Exception as e:
        clean_err = re.sub(r'https?://\S+', '', str(e)).strip().strip(":")
        log(f"[CDX]    Preflight count check failed ({clean_err}) -- skipping.")


# -- Step 1: Get Snapshot List -------------------------------------------------
def get_snapshots(cfg: dict) -> list:
    cdx_url = cfg["url"] + ("*" if cfg["filter_cdx_wildcard"] else "")
    params = {
        "url": cdx_url,
        "output": "json",
        "fl": "timestamp,original",
        "collapse": "digest",
        "filter": "statuscode:200",
    }
    if cfg["from_date"]:
        params["from"] = cfg["from_date"]
    if cfg["to_date"]:
        params["to"] = cfg["to_date"]

    log(f"[CDX]    Querying snapshots for: {cdx_url}")
    for attempt in range(1, cfg["retries"] + 1):
        try:
            resp = requests.get(CDX_API, params=params, timeout=30)
            resp.raise_for_status()
            rows = resp.json()
            if not rows or len(rows) < 2:
                log("[CDX]    No snapshots found.")
                return []
            header, *data = rows
            snapshots = [dict(zip(header, row)) for row in data]
            log(f"[CDX]    Found {len(snapshots)} unique snapshots.")

            # Post-filter by filters when any filters are specified
            if cfg["filters_any"] or cfg["filters_all"]:
                before = len(snapshots)
                snapshots = [
                    s for s in snapshots
                    if url_matches_filters(
                        s["original"], cfg["filters_any"], cfg["filters_all"],
                        cfg["case_sensitive"], cfg["match_child_paths"],
                    )
                ]
                any_raw = cfg["filter_any_raw"]
                all_raw = cfg["filter_all_raw"]
                raw_display = " | ".join(p for p in [any_raw, all_raw] if p)
                log(
                    f"[CDX]    {len(snapshots)} of {before} snapshots passed "
                    f"filter(s): {raw_display!r}."
                )

            return snapshots
        except Exception as e:
            clean_err = re.sub(r'https?://\S+', '', str(e)).strip().strip(":")
            if attempt < cfg["retries"]:
                log(f"[CDX]    Query failed: {clean_err} -- retrying in {cfg['delay']}s ...")
                time.sleep(cfg["delay"])
            else:
                sys.exit(_ERRORS["cdx_failed"].format(retries=cfg["retries"], error=clean_err))


# -- Step 2: Sampling ----------------------------------------------------------
def _sample_group(snapshots: list, cfg: dict,
                  prefer_canonical: bool = True) -> tuple:
    """
    Sample one flat list of snapshots according to frequency/anchor/min_gap.
    Returns (sampled, discarded_timestamps, fallbacks_map).

    fallbacks_map : dict  – maps each selected snapshot's timestamp to a list
                            of runner-up snapshots from the same time bucket,
                            sorted by proximity to the anchor. Capped at
                            cfg["fallback_candidates"] entries per bucket.

    prefer_canonical=True  – within a time bucket, the base URL is preferred
                             over variants as a tiebreaker (single-file mode).
    prefer_canonical=False – pure time-distance tiebreaker only (per-URL mode,
                             where all snapshots already share the same URL).
    """
    frequency       = cfg["frequency"]
    anchor          = cfg["sample_from"]
    min_gap_secs    = cfg["min_gap_secs"]
    freq_fmt        = FREQ_MAP.get(frequency)
    base_url        = cfg["url"]
    n_fallbacks     = cfg["fallback_candidates"]
    collision_priority = cfg.get("collision_priority", "time")

    # Build filter-rank map: original_url -> rank (lower = higher priority).
    # Only populated when collision_priority = 'filter' and we're in single-file
    # mode (prefer_canonical=True), where multiple URL variants compete.
    url_filter_rank: dict = {}
    if collision_priority == "filter" and prefer_canonical:
        filters_any = cfg.get("filters_any", [])
        any_includes = [f for f in filters_any if not f["negate"]]
        for rank, f in enumerate(any_includes):
            # Walk every snapshot to assign the rank of the first matching token
            # (snapshots list is available in the outer scope via closure)
            for snap in snapshots:
                orig = snap.get("original", "")
                if orig and orig not in url_filter_rank:
                    if _single_filter_matches(
                        orig, f["pattern"], f["mode"],
                        cfg["case_sensitive"], cfg["match_child_paths"]
                    ):
                        url_filter_rank[orig] = rank

    def sort_key(s, target):
        time_dist = abs((ts_to_dt(s["timestamp"]) - target).total_seconds())
        orig = s.get("original", "")
        if collision_priority == "filter" and prefer_canonical:
            frank = url_filter_rank.get(orig, 999)
            return (frank, time_dist)
        if prefer_canonical:
            # Legacy behaviour: base URL beats variants, then by time
            is_variant = 0 if orig == base_url else 1
            return (is_variant, time_dist)
        return time_dist

    # frequency = "all": no bucketing, just min_gap filtering, no fallbacks
    if freq_fmt is None:
        if min_gap_secs == 0:
            return snapshots, [], {}
        kept = [snapshots[0]]
        discarded = []
        for snap in snapshots[1:]:
            if abs((ts_to_dt(snap["timestamp"]) -
                    ts_to_dt(kept[-1]["timestamp"])).total_seconds()) >= min_gap_secs:
                kept.append(snap)
            else:
                discarded.append(snap["timestamp"])
        return kept, discarded, {}

    # Bucket by period, sort each group by proximity to anchor, pick best per bucket
    buckets: dict = {}
    for snap in snapshots:
        bucket = ts_to_dt(snap["timestamp"]).strftime(freq_fmt)
        buckets.setdefault(bucket, []).append(snap)

    bucket_sorted: dict = {}
    sampled = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        ref_dt = ts_to_dt(group[0]["timestamp"])
        target = anchor_dt_for(ref_dt, frequency, anchor)
        sorted_group = sorted(group, key=lambda s: sort_key(s, target))
        bucket_sorted[bucket] = sorted_group
        sampled.append(sorted_group[0])

    # Min-gap pass
    discarded = []
    if min_gap_secs > 0 and len(sampled) > 1:
        kept = [sampled[0]]
        for snap in sampled[1:]:
            prev_dt = ts_to_dt(kept[-1]["timestamp"])
            curr_dt = ts_to_dt(snap["timestamp"])
            gap = abs((curr_dt - prev_dt).total_seconds())
            if gap >= min_gap_secs:
                kept.append(snap)
            else:
                prev_anchor = anchor_dt_for(prev_dt, frequency, anchor)
                curr_anchor = anchor_dt_for(curr_dt, frequency, anchor)
                prev_dist = abs((ts_to_dt(kept[-1]["timestamp"]) - prev_anchor).total_seconds())
                curr_dist = abs((curr_dt - curr_anchor).total_seconds())
                if prefer_canonical:
                    if collision_priority == "filter":
                        prev_frank = url_filter_rank.get(kept[-1].get("original", ""), 999)
                        curr_frank = url_filter_rank.get(snap.get("original", ""), 999)
                        curr_better = (prev_frank, prev_dist) > (curr_frank, curr_dist)
                    else:
                        prev_is_variant = kept[-1]["original"] != base_url
                        curr_is_variant = snap["original"] != base_url
                        curr_better = (prev_is_variant, prev_dist) > (curr_is_variant, curr_dist)
                else:
                    curr_better = curr_dist < prev_dist
                if curr_better:
                    discarded.append(kept[-1]["timestamp"])
                    kept[-1] = snap
                else:
                    discarded.append(snap["timestamp"])
        sampled = kept

    # Build fallback lists after min_gap so we use the final winner per bucket.
    # Fallbacks are the other sorted candidates from the same bucket, capped at n_fallbacks.
    fallbacks_map: dict = {}
    if n_fallbacks > 0:
        for snap in sampled:
            bucket = ts_to_dt(snap["timestamp"]).strftime(freq_fmt)
            remaining = [c for c in bucket_sorted.get(bucket, [])
                         if c["timestamp"] != snap["timestamp"]]
            if remaining:
                fallbacks_map[snap["timestamp"]] = remaining[:n_fallbacks]

    return sampled, discarded, fallbacks_map


def _format_gap(min_gap_secs: int) -> str:
    gap_mins  = min_gap_secs // 60
    gap_hours = gap_mins // 60
    gap_days  = gap_hours // 24
    if gap_days >= 1:
        return f"{gap_days}d {gap_hours % 24}h" if gap_hours % 24 else f"{gap_days}d"
    if gap_hours >= 1:
        return f"{gap_hours}h {gap_mins % 60}m" if gap_mins % 60 else f"{gap_hours}h"
    return f"{gap_mins}m"


def sample_snapshots(snapshots: list, cfg: dict) -> tuple:
    """
    Returns (sampled_snapshots, fallbacks_map).
    fallbacks_map maps each snapshot's timestamp to its ordered list of fallback
    candidates from the same time bucket.
    """
    frequency    = cfg["frequency"]
    anchor       = cfg["sample_from"]
    min_gap_secs = cfg["min_gap_secs"]
    per_url_mode = cfg["split_output"] != "no" and cfg["filter_cdx_wildcard"]

    if per_url_mode:
        # Sample each distinct URL independently so no URL loses a time bucket
        # to another URL's snapshot. Results are merged and re-sorted after.
        by_url: dict = {}
        for snap in snapshots:
            by_url.setdefault(snap["original"], []).append(snap)

        all_sampled   = []
        all_discarded = []
        combined_fallbacks: dict = {}
        for url_snaps in by_url.values():
            sampled, discarded, fb = _sample_group(url_snaps, cfg, prefer_canonical=False)
            all_sampled.extend(sampled)
            all_discarded.extend(discarded)
            combined_fallbacks.update(fb)

        all_sampled.sort(key=lambda s: s["timestamp"])
        log(f"[Sample] '{frequency}' ({anchor}) -> {len(all_sampled)} snapshots selected "
            f"across {len(by_url)} URL(s).")
        if all_discarded and min_gap_secs > 0:
            dates_str = ", ".join(
                ts_to_dt(ts).strftime("%Y-%m-%d") for ts in sorted(all_discarded)
            )
            log(f"[Gap]    {len(all_discarded)} snapshot(s) discarded "
                f"(min gap: {_format_gap(min_gap_secs)}): {dates_str}")
        return all_sampled, combined_fallbacks

    else:
        sampled, discarded, fallbacks_map = _sample_group(snapshots, cfg, prefer_canonical=True)
        log(f"[Sample] '{frequency}' ({anchor}) -> {len(sampled)} snapshots selected.")
        if discarded and min_gap_secs > 0:
            dates_str = ", ".join(
                ts_to_dt(ts).strftime("%Y-%m-%d") for ts in discarded
            )
            log(f"[Gap]    {len(discarded)} snapshot(s) discarded "
                f"(min gap: {_format_gap(min_gap_secs)}): {dates_str}")
        return sampled, fallbacks_map


# -- Step 3: Fetch One Snapshot ------------------------------------------------
def _check_playwright():
    """Exit with a helpful message if the playwright package is not installed."""
    global _playwright_available
    if _playwright_available is not None:
        return
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        _playwright_available = True
    except ImportError:
        _error_exit(_ERRORS["playwright_missing"])


# Playwright browser runs in a single dedicated thread. All fetch requests are
# submitted via _browser_queue and results returned through per-request Futures.
# This avoids the greenlet thread-affinity crash that occurs when Playwright's
# sync API is called from multiple ThreadPoolExecutor worker threads.
import queue as _queue
import concurrent.futures as _cf

_pw_lock             = threading.Lock()
_browser_queue       = _queue.Queue()
_browser_threads     = []
_browsers_ready      = 0
_browsers_ready_lock = threading.Lock()
_all_browsers_ready  = threading.Event()
_browser_error       = None   # set if any browser thread fails to start
_expected_browsers   = 0


def _browser_worker():
    """Runs in a dedicated thread. Owns its own Playwright + Chromium lifecycle."""
    global _browser_error
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().__enter__()
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                log("[Setup]  Chromium not found -- downloading via 'playwright install chromium' ...")
                import subprocess
                result = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"'playwright install chromium' failed:\n{result.stderr.strip()}"
                    )
                log("[Setup]  Chromium installed successfully.")
                browser = pw.chromium.launch(headless=True)
            else:
                raise
        with _browsers_ready_lock:
            global _browsers_ready
            _browsers_ready += 1
            if _browsers_ready >= _expected_browsers:
                _all_browsers_ready.set()
    except Exception as e:
        _browser_error = e
        _all_browsers_ready.set()
        return

    while True:
        item = _browser_queue.get()
        if item is None:          # shutdown signal
            break
        wayback_url, selectors, future = item
        try:
            page = browser.new_page()
            try:
                page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf,eot,css}",
                    lambda route, _: route.abort(),
                )
                page.route(
                    "**/web.archive.org/static/**",
                    lambda route, _: route.abort(),
                )
                # Wait for the page load event first (reliable), then give JS
                # up to 10 extra seconds to finish its API calls. If the target
                # selector appears sooner we bail out early; if no CSS selector
                # is available (bare nth-child steps only) we fall back to
                # networkidle. Both timeouts are soft - we proceed regardless.
                resp = page.goto(wayback_url, wait_until="load", timeout=30_000)
                if resp is None or not resp.ok:
                    status = resp.status if resp else "no response"
                    raise RuntimeError(f"HTTP {status}")
                try:
                    if selectors:
                        page.wait_for_selector(
                            ", ".join(dict.fromkeys(selectors)),
                            timeout=10_000,
                        )
                    else:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass  # proceed with whatever JS has run so far
                future.set_result(page.content())
            except Exception as e:
                future.set_exception(e)
            finally:
                page.close()
        except Exception as e:
            if not future.done():
                future.set_exception(e)

    try:
        browser.close()
        pw.__exit__(None, None, None)
    except Exception:
        pass


def _get_browser(n: int = 1):
    """Start n dedicated browser threads if not already running, then wait for all to be ready."""
    global _expected_browsers
    with _pw_lock:
        if not _browser_threads:
            _expected_browsers = n
            for _ in range(n):
                t = threading.Thread(
                    target=_browser_worker, daemon=True, name="playwright-browser"
                )
                t.start()
                _browser_threads.append(t)
    _all_browsers_ready.wait()
    if _browser_error:
        clean_err = re.sub(r'https?://\S+', '', str(_browser_error)).strip()
        _error_exit(_ERRORS["chromium_failed"].format(error=clean_err))


def _close_browser():
    """Shut down all browser threads cleanly."""
    for t in _browser_threads:
        if t.is_alive():
            _browser_queue.put(None)   # one poison pill per thread
    for t in _browser_threads:
        t.join(timeout=10)


def _fetch_html_playwright(wayback_url: str, selectors: list) -> str:
    """
    Submit a fetch job to the browser thread and block until the result is ready.
    Raises on timeout or navigation failure.
    """
    future = _cf.Future()
    _browser_queue.put((wayback_url, selectors, future))
    return future.result()   # blocks until the browser thread finishes the job


def fetch_snapshot(session, index: int, total: int, timestamp: str,
                   original_url: str, cfg: dict, buffered: bool = True,
                   fallbacks: list = None) -> dict:
    """
    Fetch a snapshot, trying fallback candidates if the primary fails with a
    definitive error (404/403) or exhausts all retries.

    Fallbacks are snapshots from the same time bucket sorted by proximity to
    the anchor, so they are always within the same frequency period.
    """
    candidates = [{"timestamp": timestamp, "original": original_url}]
    if fallbacks:
        candidates.extend(fallbacks)

    emit = (lambda idx, m: buffer_and_flush(idx, m, total)) if buffered else lambda idx, m: log(m)
    max_sel_len = max(len(e["selector"]) for e in cfg["elements"])

    # Keep track of the primary date/time for the failure return dict
    primary_date_str, primary_time_str = format_datetime(ts_to_dt(timestamp), cfg)
    last_err = ""

    # Accumulates retry/fallback notices so they're bundled into one emit() call,
    # preventing premature buffer flushes from other threads from orphaning output.
    extra_lines = []

    for cand_idx, candidate in enumerate(candidates):
        curr_ts  = candidate["timestamp"]
        curr_url = candidate["original"]
        wayback_url = f"{WAYBACK_BASE}/{curr_ts}/{curr_url}"
        date_str, time_str = format_datetime(ts_to_dt(curr_ts), cfg)
        prefix = f"[{index}/{total}] {date_str} {time_str}".strip()

        if cand_idx > 0:
            notice = f"  -> trying fallback {cand_idx}/{len(candidates) - 1}: {curr_ts}"
            if buffered:
                extra_lines.append(notice)
            else:
                log(notice)

        hit_definitive = False

        for attempt in range(1, cfg["retries"] + 1):
            try:
                if cfg["headless_browser"]:
                    selectors = [
                        e["selector_chain"][0]["sel"]
                        for e in cfg["elements"]
                        if e["selector_chain"][0]["sel"] is not None
                    ]
                    html_text = _fetch_html_playwright(wayback_url, selectors)
                else:
                    resp = session.get(
                        wayback_url, timeout=15,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    )
                    resp.raise_for_status()
                    html_text = resp.text

                soup = BeautifulSoup(html_text, "lxml")
                elem_values = {}
                lines = [prefix]
                if buffered:
                    lines.extend(extra_lines)

                # First pass: traverse selectors and collect values for every element
                elem_results = []
                for elem in cfg["elements"]:
                    sel_chain = elem["selector_chain"]
                    extract = elem["extract"]

                    current_scope = [soup]
                    for step in sel_chain:
                        sel = step["sel"]
                        nth = step["nth"]
                        next_scope = []
                        for scope in current_scope:
                            if sel is None:
                                children = [c for c in scope.children
                                            if hasattr(c, "name") and c.name]
                                if nth is not None and 1 <= nth <= len(children):
                                    next_scope.append(children[nth - 1])
                            else:
                                found = scope.select(sel)
                                if nth is not None:
                                    if 1 <= nth <= len(found):
                                        next_scope.append(found[nth - 1])
                                else:
                                    next_scope.extend(found)
                        current_scope = next_scope
                    matches = current_scope
                    if not matches:
                        raw_values = None   # sentinel: element not found
                    else:
                        raw_values = [v for m in matches
                                      for v in [extract_value(m, extract)] if v]
                    elem_results.append((elem, raw_values))

                # Compute the max label column width so colons align across all
                # elements and all index variants.  For multi-value elements the
                # label is "selector[N]", so factor in the bracket width.
                max_label_width = max_sel_len
                for elem, raw_values in elem_results:
                    if raw_values and len(raw_values) > 1:
                        bracket_width = len(f"[{len(raw_values)}]")
                        max_label_width = max(
                            max_label_width,
                            len(elem["selector"]) + bracket_width,
                        )

                # Second pass: record elem_values and build console lines
                for elem, raw_values in elem_results:
                    sel_display = elem["selector"]
                    if raw_values is None:
                        elem_values[elem["slot"]] = []
                        label = f"  {sel_display:<{max_label_width}}"
                        lines.append(f"{label}: (no element)")
                    elif not raw_values:
                        elem_values[elem["slot"]] = []
                        label = f"  {sel_display:<{max_label_width}}"
                        lines.append(f"{label}: (blank)")
                    elif len(raw_values) == 1:
                        elem_values[elem["slot"]] = raw_values
                        label = f"  {sel_display:<{max_label_width}}"
                        lines.append(f"{label}: {raw_values[0]}")
                    else:
                        elem_values[elem["slot"]] = raw_values
                        for idx, v in enumerate(raw_values, 1):
                            indexed_name = f"{sel_display}[{idx}]"
                            label = f"  {indexed_name:<{max_label_width}}"
                            lines.append(f"{label}: {v}")

                emit(index, "\n".join(lines))
                return {
                    "timestamp": curr_ts,
                    "date": date_str,
                    "time": time_str,
                    "elem_values": elem_values,
                    "url": wayback_url,
                    "original": curr_url,
                    "error": "",
                }

            except requests.exceptions.Timeout:
                last_err = "timeout"
            except requests.exceptions.HTTPError as e:
                last_err = f"HTTP {e.response.status_code}"
                if e.response.status_code in (404, 403):
                    hit_definitive = True
                    break   # skip remaining retries, move to next candidate
            except RuntimeError as e:
                # Normalise browser errors to match HTTP mode formatting
                raw_err = str(e).splitlines()[0]
                if "timeout" in raw_err.lower():
                    last_err = "timeout"
                elif "HTTP" in raw_err:
                    match = re.search(r'HTTP \d+', raw_err)
                    last_err = match.group(0) if match else re.sub(r'https?://\S+', '', raw_err).strip()
                else:
                    last_err = re.sub(r'https?://\S+', '', raw_err).strip()
                if "HTTP 404" in last_err or "HTTP 403" in last_err:
                    hit_definitive = True
                    break
            except Exception as e:
                # Normalise generic exceptions for consistency
                raw_err = str(e).splitlines()[0]
                if "timeout" in raw_err.lower():
                    last_err = "timeout"
                else:
                    last_err = re.sub(r'https?://\S+', '', raw_err).strip()

            if attempt < cfg["retries"] and not hit_definitive:
                retry_notice = f"  -> attempt {attempt}/{cfg['retries']} failed: {last_err} -- retrying ..."
                if buffered:
                    extra_lines.append(retry_notice)
                else:
                    log(f"{prefix}\n{retry_notice}")
                time.sleep(cfg["delay"])

        # Move to next candidate if one exists
        if cand_idx < len(candidates) - 1:
            continue
        break  # all candidates exhausted

    # All candidates failed - report using the primary snapshot's info
    primary_prefix = f"[{index}/{total}] {primary_date_str} {primary_time_str}".strip()
    if buffered and extra_lines:
        failure_lines = [primary_prefix] + extra_lines + [f"... failed ({last_err})"]
        emit(index, "\n".join(failure_lines))
    else:
        emit(index, f"{primary_prefix} ... failed ({last_err})")
    return {
        "timestamp": timestamp,
        "date": primary_date_str,
        "time": primary_time_str,
        "elem_values": {elem["slot"]: [] for elem in cfg["elements"]},
        "url": f"{WAYBACK_BASE}/{timestamp}/{original_url}",
        "original": original_url,
        "error": last_err,
    }


# -- Result Padding ------------------------------------------------------------
def apply_padding(results: list, cfg: dict) -> list:
    """
    When result_padding is enabled and a regular frequency is in use, return a
    new list that inserts blank entries for every period bucket that had no valid
    snapshot, so the output spans every period continuously between the first and
    last result. Returns the original list unchanged if result_padding is not applicable.
    """
    frequency = cfg["frequency"]
    if not cfg["result_padding"] or frequency == "all":
        return results

    freq_fmt = FREQ_MAP[frequency]
    elements = cfg["elements"]

    bucket_map: dict = {}
    for r in results:
        if r and r.get("timestamp"):
            key = ts_to_dt(r["timestamp"]).strftime(freq_fmt)
            bucket_map[key] = r

    timestamps = [
        ts_to_dt(r["timestamp"])
        for r in results
        if r and r.get("timestamp")
    ]
    if not timestamps:
        return results

    first_dt = min(timestamps)
    last_dt  = max(timestamps)
    padded = []
    for period_dt in iter_periods(first_dt, last_dt, frequency):
        key = period_dt.strftime(freq_fmt)
        if key in bucket_map:
            padded.append(bucket_map[key])
        else:
            anchor_dt = anchor_dt_for(period_dt, frequency, cfg["sample_from"])
            date_str, time_str = format_datetime(anchor_dt, cfg)
            padded.append({
                "timestamp": "",
                "date": date_str,
                "time": time_str,
                "elem_values": {elem["slot"]: [] for elem in elements},
                "url": "",
                "error": "",
            })
    return padded


# -- Output Path Resolution ----------------------------------------------------
def resolve_output_path(path: str, override: bool) -> str:
    """
    Return *path* unchanged when override is True or the file doesn't exist.
    When override is False and the file already exists, append an incrementing
    counter suffix before the extension until a free filename is found.
    E.g. wayback_results.csv -> wayback_results_1.csv -> wayback_results_2.csv
    """
    if override or not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


# -- Output Folder Structure ---------------------------------------------------
def get_output_dirs(output: str) -> tuple:
    """
    Derive the three output directories from the configured output path.

      base_dir : folder named after the output stem, e.g. 'wayback_results/'
      raw_dir  : base_dir/raw/      – regular CSV files
      ref_dir  : base_dir/reformatted/ – reformatted CSV files
    """
    parent   = os.path.dirname(output) or "."
    stem     = os.path.splitext(os.path.basename(output))[0]
    base_dir = os.path.join(parent, stem)
    raw_dir  = os.path.join(base_dir, "raw")
    ref_dir  = os.path.join(base_dir, "reformatted")
    return base_dir, raw_dir, ref_dir


# -- Step 4: Write CSV ---------------------------------------------------------
def write_merged_csv(groups: dict, cfg: dict, output_path: str) -> None:
    """
    Write all groups into a single CSV file.

    rows layout:
        Shared date/time header across the top.  For each group, in order:
          - a blank label row carrying the group suffix as the first cell
          - url (suffix) row
          - error (suffix) row
          - one element row per tracked element, labelled "selector (extract) (suffix)"
        All rows share the same date columns.

    columns layout:
        Each group is stacked below the previous, separated by a blank row.
        A group-label row (suffix in col 0) appears before each block's
        column-header row, so every block is self-identifying.
    """
    if not groups:
        log("[Merged]  No groups to write.")
        return

    layout    = cfg["csv_layout"]
    show_time = cfg["show_time"]
    elements  = cfg["elements"]

    padded_groups: list = []
    for suffix, group_results in groups.items():
        padded_groups.append((suffix, apply_padding(group_results, cfg)))

    if layout == "rows":
        # Build unified sorted date list
        all_dates: list = []
        seen_dates: set = set()
        for _, g_results in padded_groups:
            for r in g_results:
                d = r["date"] if r else ""
                if d and d not in seen_dates:
                    seen_dates.add(d)
                    all_dates.append((r["timestamp"] if r else "", d))
        all_dates.sort(key=lambda x: x[0])
        date_order = [d for _, d in all_dates]

        def group_lookup(g_results):
            return {r["date"]: r for r in g_results if r and r.get("date")}

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            writer.writerow(["date"] + date_order)
            if show_time:
                lookups = [group_lookup(g) for _, g in padded_groups]
                time_row = ["time"]
                for d in date_order:
                    val = next((lk[d]["time"] for lk in lookups if d in lk), "")
                    time_row.append(val)
                writer.writerow(time_row)

            for suffix, g_results in padded_groups:
                lk = group_lookup(g_results)

                # Group label row (suffix in col 0, rest blank)
                writer.writerow([suffix])

                # url and error labelled with suffix
                writer.writerow([f"url ({suffix})"] +
                                 [lk[d]["url"]   if d in lk else "" for d in date_order])
                writer.writerow([f"error ({suffix})"] +
                                 [lk[d]["error"] if d in lk else "" for d in date_order])

                # Element rows
                max_per_slot = {}
                for elem in elements:
                    slot = elem["slot"]
                    max_per_slot[slot] = max(
                        (len(r["elem_values"].get(slot, [])) for r in g_results if r),
                        default=1
                    )
                    max_per_slot[slot] = max(max_per_slot[slot], 1)

                for elem in elements:
                    slot  = elem["slot"]
                    sel   = elem["selector"]
                    ext   = elem["extract"]
                    count = max_per_slot[slot]
                    for i in range(count):
                        base_label = (f"{sel} ({ext})" if count == 1
                                      else f"{sel} [{i+1}] ({ext})")
                        row_label = f"{base_label} ({suffix})"
                        row = [row_label]
                        for d in date_order:
                            r    = lk.get(d)
                            vals = r["elem_values"].get(slot, []) if r else []
                            row.append(vals[i] if i < len(vals) else "")
                        writer.writerow(row)

    else:  # columns: stack groups vertically
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            first = True
            for suffix, g_results in padded_groups:
                if not first:
                    writer.writerow([])
                first = False

                descriptors = []
                descriptors.append(("date", lambda r, _=None: [r["date"]]))
                if show_time:
                    descriptors.append(("time", lambda r, _=None: [r["time"]]))
                descriptors.append((f"url ({suffix})",   lambda r, _=None: [r["url"]]))
                descriptors.append((f"error ({suffix})", lambda r, _=None: [r["error"]]))

                max_per_slot = {}
                for elem in elements:
                    slot = elem["slot"]
                    max_per_slot[slot] = max(
                        (len(r["elem_values"].get(slot, [])) for r in g_results if r),
                        default=1
                    )
                    max_per_slot[slot] = max(max_per_slot[slot], 1)

                for elem in elements:
                    slot  = elem["slot"]
                    sel   = elem["selector"]
                    ext   = elem["extract"]
                    count = max_per_slot[slot]
                    label_base = f"{sel} ({ext})"
                    if count == 1:
                        def make_fn(s):
                            return lambda r: r["elem_values"].get(s, [""])[:1] or [""]
                        descriptors.append((label_base, make_fn(slot)))
                    else:
                        for i in range(count):
                            lbl = f"{sel} [{i+1}] ({ext})"
                            def make_fn(s, idx):
                                return lambda r: [(r["elem_values"].get(s, []) + [""] * (idx + 1))[idx]]
                            descriptors.append((lbl, make_fn(slot, i)))

                # Group label row then column headers
                writer.writerow([suffix] + [""] * (len(descriptors) - 1))
                writer.writerow([label for label, _ in descriptors])
                for r in g_results:
                    writer.writerow([fn(r)[0] for _, fn in descriptors])

    total = sum(len(g) for _, g in padded_groups)
    log(f"\n[Merged]  Saved {total} total snapshots ({len(padded_groups)} group(s))"
        f" -> {os.path.abspath(output_path)}")


def url_slug(label: str) -> str:
    """
    If *label* looks like a URL or path, return just the final non-empty path
    segment (everything after the last '/', with query strings and fragments
    stripped).  Otherwise return the label unchanged.

    Handles three forms:
      - Absolute URLs  (http:// or https:// or //)
      - Root-relative paths (/web/20230101/https://example.com/page)
        — the form Wayback Machine uses when rewriting href attributes
      - Plain strings with no path structure — returned unchanged

    Examples
    --------
    https://example.com/products/widget-pro?ref=nav     ->  widget-pro
    /web/20230101000000/https://example.com/products/p  ->  p
    https://example.com/section/                        ->  section
    https://example.com/                                ->  example.com  (hostname fallback)
    not-a-url                                           ->  not-a-url
    """
    s = label.strip()
    if not s:
        return s
    is_absolute = s.startswith("http://") or s.startswith("https://") or s.startswith("//")
    is_relative = not is_absolute and s.startswith("/")
    if not is_absolute and not is_relative:
        return s
    try:
        if is_absolute:
            parsed = urlparse(s)
            path = parsed.path.rstrip("/")
            if path:
                segment = path.rsplit("/", 1)[-1]
                if segment:
                    segment = unquote(segment)
                    dot = segment.rfind(".")
                    if dot > 0:
                        ext = segment[dot + 1:]
                        if ext.isalpha() and 2 <= len(ext) <= 5:
                            segment = segment[:dot]
                    return segment
            host = parsed.netloc or parsed.path
            return host if host else s
        else:
            # Root-relative path: strip query string and fragment, then take
            # the last non-empty segment.  This handles Wayback Machine's
            # /web/TIMESTAMP/ORIGINAL_URL rewritten href format.
            path = s.split("?")[0].split("#")[0].rstrip("/")
            if path and path != "/":
                segment = path.rsplit("/", 1)[-1]
                if segment:
                    segment = unquote(segment)
                    dot = segment.rfind(".")
                    if dot > 0:
                        ext = segment[dot + 1:]
                        if ext.isalpha() and 2 <= len(ext) <= 5:
                            segment = segment[:dot]
                    return segment
    except Exception:
        pass
    return s


def _merge_key(s: str) -> str:
    """Return a normalized merge key: lowercase alphanumeric only.
    Used to detect labels that are the same modulo case, whitespace, and separators.
    e.g. 'Value-1', 'value 1', 'VALUE_1' all map to 'value1'.
    """
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _apply_label_case(s: str, mode: str, strip_separators: bool = False) -> str:
    """Apply a case transformation to a label string for display in the output.
    If strip_separators is True, replaces '-' and '_' characters with spaces first."""
    if strip_separators:
        s = s.replace("-", " ").replace("_", " ")
    if mode == "lower":
        return s.lower()
    if mode == "upper":
        return s.upper()
    if mode == "sentence":
        return s[:1].upper() + s[1:].lower() if s else s
    return s  # default: no transformation


def reformat_merged_csv(groups: dict, cfg: dict, output_path: str) -> None:
    """
    Write a merged reformatted CSV for all groups into one file.

    rows layout:
        Shared date/time header.  Then, depending on merged_meta:
          interleaved : for each group - group-label row, url row, error row,
                        then that group's data rows - all in one pass.
          grouped     : all groups' data rows first (each preceded by its
                        group-label row), then all url rows, then all error
                        rows, at the bottom.  url/error rows are labelled
                        "url (suffix)" / "error (suffix)".

    columns layout:
        Groups stacked vertically with blank separator.  Each block starts
        with a group-label row, then date/time, then depending on merged_meta:
          interleaved : url, error, then data rows.
          grouped     : data rows, then url, then error at the bottom of the block.
        url/error rows are labelled "url (suffix)" / "error (suffix)".
    """
    if not groups:
        return

    layout      = cfg["csv_layout"]
    show_time   = cfg["show_time"]
    zero_fill   = cfg["zero_fill"]
    fill_first  = cfg["fill_first"]
    pairs       = cfg["reformat_pairs"]
    sort_mode   = cfg["sort"]
    merged_meta = cfg.get("merged_meta", "grouped")
    tracked_slots = {e["slot"] for e in cfg["elements"]}

    ref_dir      = cfg.get("ref_dir", os.path.dirname(output_path) or ".")
    ref_stem     = os.path.splitext(os.path.basename(output_path))[0] + "_reformatted"
    ref_path_raw = os.path.join(ref_dir, ref_stem + ".csv")
    ref_path     = resolve_output_path(ref_path_raw, cfg["file_override"])
    if ref_path != ref_path_raw:
        log(f"[Reformat] '{ref_path_raw}' already exists -- writing to '{ref_path}' instead.")

    for label_slot, value_slot in pairs:
        if label_slot not in tracked_slots or value_slot not in tracked_slots:
            log(f"[Reformat] Skipping merged reformat: required slot not tracked.")
            return

    def _norm(lbl):
        return url_slug(lbl)

    do_merge   = cfg.get("label_merge", False)
    label_case = cfg.get("label_case", "default")
    strip_seps = cfg.get("label_strip_separators", False)

    def build_group_data(g_results):
        g_results = apply_padding(g_results, cfg)
        snap_dates  = [r["date"]  if r else "" for r in g_results]
        snap_times  = [r["time"]  if r else "" for r in g_results]
        snap_urls   = [r["url"]   if r else "" for r in g_results]
        snap_errors = [r["error"] if r else "" for r in g_results]

        pair_data = []
        for label_slot, value_slot in pairs:
            seen_labels, seen_set = [], set()
            merge_key_to_display: dict = {}
            for r in g_results:
                if not r: continue
                for lbl in r["elem_values"].get(label_slot, []):
                    norm = _norm(lbl)
                    if not norm:
                        continue
                    if do_merge:
                        mkey = _merge_key(norm)
                        if mkey and mkey not in seen_set:
                            display = _apply_label_case(norm, label_case, strip_seps)
                            seen_labels.append(display)
                            seen_set.add(mkey)
                            merge_key_to_display[mkey] = display
                    else:
                        display = _apply_label_case(norm, label_case, strip_seps)
                        if display not in seen_set:
                            seen_labels.append(display); seen_set.add(display)
            if sort_mode == "alphabet":
                ordered = sorted(seen_labels, key=lambda x: x.lower())
            elif sort_mode == "reverse":
                ordered = sorted(seen_labels, key=lambda x: x.lower(), reverse=True)
            else:
                ordered = seen_labels
            snap_maps = []
            for r in g_results:
                if not r: snap_maps.append({}); continue
                lbls = [_norm(l) for l in r["elem_values"].get(label_slot, [])]
                vals = r["elem_values"].get(value_slot, [])
                snap_map: dict = {}
                for i, slug_key in enumerate(lbls):
                    if do_merge:
                        mkey = _merge_key(slug_key)
                        display = merge_key_to_display.get(mkey)
                        if display is not None and display not in snap_map:
                            snap_map[display] = vals[i] if i < len(vals) else ""
                    else:
                        display = _apply_label_case(slug_key, label_case, strip_seps)
                        if display and display not in snap_map:
                            snap_map[display] = vals[i] if i < len(vals) else ""
                snap_maps.append(snap_map)
            pair_data.append((ordered, snap_maps))

        zero_cols: dict = {}
        if zero_fill != "no":
            for ordered, snap_maps in pair_data:
                for label in ordered:
                    first = next((i for i, m in enumerate(snap_maps) if m.get(label)), None)
                    if first is None:
                        zero_cols[label] = None
                    elif first == 0:
                        zero_cols[label] = -1 if fill_first else None
                    else:
                        zero_cols[label] = (
                            next((i for i in range(first-1,-1,-1)
                                  if g_results[i] and g_results[i].get("timestamp")), first-1)
                            if zero_fill == "snapshot" else first - 1
                        )
            if any(v == -1 for v in zero_cols.values()):
                first_real = next((r for r in g_results if r and r.get("timestamp")), None)
                if first_real:
                    prev_dt = prev_period_dt(ts_to_dt(first_real["timestamp"]), cfg["frequency"])
                    prev_date, prev_time = format_datetime(prev_dt, cfg)
                else:
                    prev_date, prev_time = "", ""
                snap_dates  = [prev_date] + snap_dates
                snap_times  = [prev_time] + snap_times
                snap_urls   = [""] + snap_urls
                snap_errors = [""] + snap_errors
                pair_data   = [(ol, [{}] + sm) for ol, sm in pair_data]
                zero_cols   = {lbl: (0 if v == -1 else v+1 if v is not None else None)
                               for lbl, v in zero_cols.items()}

        return snap_dates, snap_times, snap_urls, snap_errors, pair_data, zero_cols

    group_data = [(suffix, build_group_data(g)) for suffix, g in groups.items()]

    with open(ref_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if layout == "rows":
            # Build unified date order from all groups
            all_date_ts: list = []
            seen: set = set()
            for _, (sdates, *_) in group_data:
                for d in sdates:
                    if d and d not in seen:
                        seen.add(d); all_date_ts.append(d)
            date_order = all_date_ts

            writer.writerow(["date"] + date_order)
            if show_time:
                time_lookup: dict = {}
                for _, (sdates, stimes, *_) in group_data:
                    for d, t in zip(sdates, stimes):
                        if d and d not in time_lookup:
                            time_lookup[d] = t
                writer.writerow(["time"] + [time_lookup.get(d, "") for d in date_order])

            if merged_meta == "interleaved":
                # Per group: label, url, error, then data rows
                for suffix, (sdates, _, surls, serrs, pdata, zero_cols) in group_data:
                    date_idx = {d: i for i, d in enumerate(sdates)}
                    writer.writerow([suffix])
                    writer.writerow([f"url ({suffix})"] +
                                    [surls[date_idx[d]] if d in date_idx else "" for d in date_order])
                    writer.writerow([f"error ({suffix})"] +
                                    [serrs[date_idx[d]] if d in date_idx else "" for d in date_order])
                    for ordered, snap_maps in pdata:
                        for label in ordered:
                            zc  = zero_cols.get(label) if zero_fill != "no" else None
                            row = []
                            for d in date_order:
                                if d in date_idx:
                                    i = date_idx[d]
                                    row.append("0" if zc is not None and i == zc
                                               else snap_maps[i].get(label, ""))
                                else:
                                    row.append("")
                            writer.writerow([f"{label} ({suffix})"] + row)

            else:  # grouped: all data first, then all urls, then all errors
                for suffix, (sdates, _, surls, serrs, pdata, zero_cols) in group_data:
                    date_idx = {d: i for i, d in enumerate(sdates)}
                    writer.writerow([suffix])
                    for ordered, snap_maps in pdata:
                        for label in ordered:
                            zc  = zero_cols.get(label) if zero_fill != "no" else None
                            row = []
                            for d in date_order:
                                if d in date_idx:
                                    i = date_idx[d]
                                    row.append("0" if zc is not None and i == zc
                                               else snap_maps[i].get(label, ""))
                                else:
                                    row.append("")
                            writer.writerow([f"{label} ({suffix})"] + row)

                writer.writerow([])  # blank separator before meta rows
                for suffix, (sdates, _, surls, serrs, *_) in group_data:
                    date_idx = {d: i for i, d in enumerate(sdates)}
                    writer.writerow([f"url ({suffix})"] +
                                    [surls[date_idx[d]] if d in date_idx else "" for d in date_order])
                for suffix, (sdates, _, surls, serrs, *_) in group_data:
                    date_idx = {d: i for i, d in enumerate(sdates)}
                    writer.writerow([f"error ({suffix})"] +
                                    [serrs[date_idx[d]] if d in date_idx else "" for d in date_order])

        else:  # columns: stack each group's block vertically
            first_block = True
            for suffix, (sdates, stimes, surls, serrs, pdata, zero_cols) in group_data:
                if not first_block:
                    writer.writerow([])
                first_block = False
                n = len(sdates)

                def data_rows():
                    for ordered, snap_maps in pdata:
                        for label in ordered:
                            zc  = zero_cols.get(label) if zero_fill != "no" else None
                            row = []
                            for i in range(n):
                                row.append("0" if zc is not None and i == zc
                                           else snap_maps[i].get(label, ""))
                            writer.writerow([label] + row)

                # Group label + date/time always first
                writer.writerow([suffix])
                writer.writerow(["date"] + sdates)
                if show_time:
                    writer.writerow(["time"] + stimes)

                if merged_meta == "interleaved":
                    writer.writerow([f"url ({suffix})"]   + surls)
                    writer.writerow([f"error ({suffix})"] + serrs)
                    data_rows()
                else:  # grouped: data then meta at bottom of block
                    data_rows()
                    writer.writerow([f"url ({suffix})"]   + surls)
                    writer.writerow([f"error ({suffix})"] + serrs)

    log(f"[Reformat] Saved merged reformatted CSV -> {os.path.abspath(ref_path)}")


def write_csv(results: list, cfg: dict, output_path: str) -> None:
    if not results:
        log("[CSV]    No results to write.")
        return

    show_time = cfg["show_time"]
    elements = cfg["elements"]
    layout = cfg["csv_layout"]

    actual_count = len(results)
    results = apply_padding(results, cfg)

    # Each descriptor: (label, fn(result) -> list of values)
    descriptors = []
    descriptors.append(("date", lambda r, _=None: [r["date"]]))
    if show_time:
        descriptors.append(("time", lambda r, _=None: [r["time"]]))
    descriptors.append(("url", lambda r, _=None: [r["url"]]))
    descriptors.append(("error", lambda r, _=None: [r["error"]]))

    # Pre-compute max matches per slot so column count is consistent
    max_per_slot = {}
    for elem in elements:
        slot = elem["slot"]
        max_per_slot[slot] = max(
            (len(r["elem_values"].get(slot, [])) for r in results),
            default=1
        )
        max_per_slot[slot] = max(max_per_slot[slot], 1)

    for elem in elements:
        slot = elem["slot"]
        sel = elem["selector"]
        extract = elem["extract"]
        count = max_per_slot[slot]
        label_base = f"{sel} ({extract})"
        if count == 1:

            def make_fn(s):
                return lambda r: r["elem_values"].get(s, [""])[:1] or [""]

            descriptors.append((label_base, make_fn(slot)))
        else:
            for i in range(count):
                label = f"{sel} [{i+1}] ({extract})"

                def make_fn(s, idx):
                    return lambda r: [(r["elem_values"].get(s, []) + [""] * (idx + 1))[idx]]

                descriptors.append((label, make_fn(slot, i)))

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if layout == "columns":
            writer.writerow([label for label, _ in descriptors])
            for r in results:
                writer.writerow([fn(r)[0] for _, fn in descriptors])
        else:
            for label, fn in descriptors:
                writer.writerow([label] + [fn(r)[0] for r in results])

    log(f"\n[CSV]    Saved {actual_count} snapshots -> {os.path.abspath(output_path)}")


# -- Step 5: Reformat CSV ------------------------------------------------------
def reformat_csv(results: list, cfg: dict, output_path: str) -> None:
    """
    Pivot the raw output so that each unique label value (from reformat_label_slot)
    becomes its own row (rows layout) or column (columns layout), with snapshot
    dates/times spread across columns/rows respectively.
    """
    if not results:
        log("[Reformat] No results to reformat.")
        return

    pairs           = cfg["reformat_pairs"]
    layout          = cfg["csv_layout"]
    sort_mode       = cfg["sort"]
    show_time       = cfg["show_time"]
    zero_fill       = cfg["zero_fill"]
    fill_first      = cfg["fill_first"]

    results = apply_padding(results, cfg)

    tracked_slots = {e["slot"] for e in cfg["elements"]}

    # Validate all pairs up front
    for label_slot, value_slot in pairs:
        if label_slot not in tracked_slots:
            log(f"[Reformat] Warning: label_elements={label_slot} was not tracked. Skipping reformat.")
            return
        if value_slot not in tracked_slots:
            log(f"[Reformat] Warning: value_elements={value_slot} was not tracked. Skipping reformat.")
            return

    # -- Build ordered labels and snap_maps for each pair ---------------------
    def normalise_label(lbl):
        return url_slug(lbl)

    do_merge   = cfg.get("label_merge", False)
    label_case = cfg.get("label_case", "default")
    strip_seps = cfg.get("label_strip_separators", False)

    def build_pair(label_slot, value_slot):
        all_raw_labels = [normalise_label(v)
                          for r in results if r
                          for v in r["elem_values"].get(label_slot, [])]
        all_values = [v for r in results if r for v in r["elem_values"].get(value_slot, [])]

        if do_merge:
            unique_label_count = len(set(_merge_key(l) for l in all_raw_labels if _merge_key(l)))
        else:
            unique_label_count = len(set(_apply_label_case(l, label_case, strip_seps) for l in all_raw_labels))

        if unique_label_count <= 1:
            log(f"[Reformat] Skipping pair ({label_slot}->{value_slot}): "
                f"label element has only one unique value across all snapshots.")
            return None, None
        if len(set(all_values)) <= 1:
            log(f"[Reformat] Skipping pair ({label_slot}->{value_slot}): "
                f"value element has only one unique value across all snapshots.")
            return None, None

        seen_labels: list = []
        seen_set: set = set()
        merge_key_to_display: dict = {}
        for r in results:
            if not r:
                continue
            for lbl in r["elem_values"].get(label_slot, []):
                norm = normalise_label(lbl)
                if not norm:
                    continue
                if do_merge:
                    mkey = _merge_key(norm)
                    if mkey and mkey not in seen_set:
                        display = _apply_label_case(norm, label_case, strip_seps)
                        seen_labels.append(display)
                        seen_set.add(mkey)
                        merge_key_to_display[mkey] = display
                else:
                    display = _apply_label_case(norm, label_case, strip_seps)
                    if display not in seen_set:
                        seen_labels.append(display)
                        seen_set.add(display)

        if sort_mode == "alphabet":
            ordered_labels = sorted(seen_labels, key=lambda x: x.lower())
        elif sort_mode == "reverse":
            ordered_labels = sorted(seen_labels, key=lambda x: x.lower(), reverse=True)
        else:
            ordered_labels = seen_labels

        snap_maps: list = []
        for r in results:
            if not r:
                snap_maps.append({})
                continue
            lbls = [normalise_label(l) for l in r["elem_values"].get(label_slot, [])]
            vals = r["elem_values"].get(value_slot, [])
            # When multiple raw labels collapse to the same key, keep the
            # first encountered value for that key within this snapshot.
            snap_map: dict = {}
            for i, slug_key in enumerate(lbls):
                if do_merge:
                    mkey = _merge_key(slug_key)
                    display = merge_key_to_display.get(mkey)
                    if display is not None and display not in snap_map:
                        snap_map[display] = vals[i] if i < len(vals) else ""
                else:
                    display = _apply_label_case(slug_key, label_case, strip_seps)
                    if display and display not in snap_map:
                        snap_map[display] = vals[i] if i < len(vals) else ""
            snap_maps.append(snap_map)
        return ordered_labels, snap_maps

    pair_data = []
    for label_slot, value_slot in pairs:
        ordered_labels, snap_maps = build_pair(label_slot, value_slot)
        if ordered_labels is not None:
            pair_data.append((ordered_labels, snap_maps))

    if not pair_data:
        return

    # -- Snapshot header values (dates/times) ---------------------------------
    snap_dates  = [r["date"]  if r else "" for r in results]
    snap_times  = [r["time"]  if r else "" for r in results]
    snap_urls   = [r["url"]   if r else "" for r in results]
    snap_errors = [r["error"] if r else "" for r in results]

    # -- Zero-fill pre-processing ----------------------------------------------
    zero_cols: dict = {}
    if zero_fill != "no":
        for ordered_labels, snap_maps in pair_data:
            for label in ordered_labels:
                first = next((i for i, m in enumerate(snap_maps) if m.get(label)), None)
                if first is None:
                    zero_cols[label] = None
                elif first == 0:
                    zero_cols[label] = -1 if fill_first else None
                else:
                    if zero_fill == "snapshot":
                        real_before = next(
                            (i for i in range(first - 1, -1, -1)
                             if results[i] and results[i].get("timestamp")),
                            None
                        )
                        zero_cols[label] = real_before if real_before is not None else first - 1
                    else:
                        zero_cols[label] = first - 1

        if any(v == -1 for v in zero_cols.values()):
            first_real = next((r for r in results if r and r.get("timestamp")), None)
            if first_real:
                prev_dt = prev_period_dt(ts_to_dt(first_real["timestamp"]), cfg["frequency"])
                prev_date, prev_time = format_datetime(prev_dt, cfg)
            else:
                prev_date, prev_time = "", ""

            snap_dates  = [prev_date] + snap_dates
            snap_times  = [prev_time] + snap_times
            snap_urls   = [""] + snap_urls
            snap_errors = [""] + snap_errors
            pair_data = [(ol, [{}] + sm) for ol, sm in pair_data]
            zero_cols = {
                lbl: (0 if v == -1 else v + 1 if v is not None else None)
                for lbl, v in zero_cols.items()
            }

    n = len(snap_dates)

    # -- Write output ----------------------------------------------------------
    ref_dir = cfg.get("ref_dir", os.path.dirname(output_path) or ".")
    ref_stem = os.path.splitext(os.path.basename(output_path))[0] + "_reformatted"
    ref_filename = ref_stem + ".csv"
    ref_path_raw = os.path.join(ref_dir, ref_filename)
    ref_path = resolve_output_path(ref_path_raw, cfg["file_override"])
    if ref_path != ref_path_raw:
        log(f"[Reformat] '{ref_path_raw}' already exists -- writing to '{ref_path}' instead.")

    with open(ref_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if layout == "rows":
            writer.writerow(["date"] + snap_dates)
            if show_time:
                writer.writerow(["time"] + snap_times)
            writer.writerow(["url"]   + snap_urls)
            writer.writerow(["error"] + snap_errors)
            for ordered_labels, snap_maps in pair_data:
                for label in ordered_labels:
                    row = [snap_maps[i].get(label, "") for i in range(n)]
                    if zero_fill != "no":
                        zc = zero_cols.get(label)
                        if zc is not None:
                            row[zc] = "0"
                    writer.writerow([label] + row)

        else:
            header = ["date"]
            if show_time:
                header.append("time")
            header.extend(["url", "error"])
            for ordered_labels, _ in pair_data:
                header.extend(ordered_labels)
            writer.writerow(header)

            for i in range(n):
                row = [snap_dates[i]]
                if show_time:
                    row.append(snap_times[i])
                row.extend([snap_urls[i], snap_errors[i]])
                for ordered_labels, snap_maps in pair_data:
                    for label in ordered_labels:
                        zc = zero_cols.get(label) if zero_fill != "no" else None
                        if zc is not None and i == zc:
                            row.append("0")
                        else:
                            row.append(snap_maps[i].get(label, ""))
                writer.writerow(row)

    log(f"[Reformat] Saved reformatted CSV -> {os.path.abspath(ref_path)}")


# -- Run One Pass Over Snapshot Indices ----------------------------------------
def run_pass(indices: list, snapshots: list, results: list,
             total: int, cfg: dict, buffered: bool = True,
             fallbacks_map: dict = None) -> list:
    fallbacks_map = fallbacks_map or {}
    failed = []
    with requests.Session() as session:
        if cfg["threads"] > 1 and not cfg["headless_browser"]:
            futures = {}
            with ThreadPoolExecutor(max_workers=cfg["threads"]) as executor:
                for idx, i in enumerate(indices):
                    snap = snapshots[i]
                    fut = executor.submit(
                        fetch_snapshot, session,
                        i + 1, total,
                        snap["timestamp"], snap["original"], cfg, buffered,
                        fallbacks_map.get(snap["timestamp"]),
                    )
                    futures[fut] = i
                for fut in as_completed(futures):
                    i = futures[fut]
                    result = fut.result()
                    results[i] = result
                    if result["error"]:
                        failed.append(i)
            failed.sort()
        else:
            for i in indices:
                snap = snapshots[i]
                result = fetch_snapshot(
                    session, i + 1, total,
                    snap["timestamp"], snap["original"], cfg, buffered,
                    fallbacks_map.get(snap["timestamp"]),
                )
                results[i] = result
                if result["error"]:
                    failed.append(i)
    return failed


# -- Filter / URL → Filename Suffix -------------------------------------------
def _is_wildcard_filter(f: dict) -> bool:
    """True if this filter token used a * and may match multiple distinct URLs."""
    return f["mode"] in ("all", "path_prefix", "contains")


def filter_to_suffix(f: dict) -> str:
    """
    Convert a non-wildcard filter dict to a filename-safe suffix string.
    Only called for filters where _is_wildcard_filter() is False
    (i.e. mode is 'path' or 'exact').
    """
    pattern = f["pattern"] or ""
    s = pattern.lstrip("/")
    s = re.sub(r"[^a-zA-Z0-9]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "filter"


def url_to_suffix(original_url: str, base_url: str) -> str:
    """
    Derive a filename-safe suffix from a URL by stripping the base URL
    and sanitising whatever remains (path + query).
    """
    parsed     = urlparse(original_url)
    base_path  = urlparse(base_url).path.rstrip("/")
    rest_path  = parsed.path
    if rest_path.startswith(base_path):
        rest_path = rest_path[len(base_path):].lstrip("/")
    else:
        rest_path = rest_path.lstrip("/")
    combined = rest_path + ("_" + parsed.query if parsed.query else "")
    s = re.sub(r"[^a-zA-Z0-9]", "_", combined)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "url"


# -- Main ----------------------------------------------------------------------
def main():
    start_time = time.time()
    cfg = load_settings()

    filters_any = cfg["filters_any"]
    filters_all = cfg["filters_all"]

    # -- Set up output folder structure ----------------------------------------
    base_dir, raw_dir, ref_dir = get_output_dirs(cfg["output"])
    os.makedirs(raw_dir, exist_ok=True)
    if cfg["reformat"]:
        os.makedirs(ref_dir, exist_ok=True)
    cfg["raw_dir"]  = raw_dir
    cfg["ref_dir"]  = ref_dir
    cfg["base_dir"] = base_dir

    gap_info = (_format_gap(cfg["min_gap_secs"])
                if cfg["min_gap_secs"] > 0 else "disabled")

    date_parts = []
    if cfg["show_month"]: date_parts.append("month")
    if cfg["show_day"]:   date_parts.append("day")
    if cfg["show_year"]:  date_parts.append("year")
    if cfg["show_time"]:  date_parts.append("time")
    sample_str = ", ".join(date_parts)

    log("=" * 60)
    log(f"  Wayback Element Tracker {VERSION}")
    log("=" * 60)

    def _filter_display(filters):
        if not filters:
            return ""
        parts = []
        for f in filters:
            prefix = "!" if f["negate"] else ""
            if f["mode"] == "all":
                label = f"{prefix}*"
            elif f["mode"] in ("path_prefix", "contains"):
                label = f"{prefix}{f['pattern']}*"
            else:
                label = f"{prefix}{f['pattern']}"
            parts.append(label)
        return ", ".join(parts)

    case_tag = "case-sensitive" if cfg["case_sensitive"] else "case-insensitive"
    any_display = _filter_display(filters_any)
    all_display = _filter_display(filters_all)

    if any_display or all_display:
        parts = []
        if any_display:
            parts.append(f"any=[{any_display}]")
        if all_display:
            parts.append(f"all=[{all_display}]")
        filter_suffix = f" (filter {', '.join(parts)}, {case_tag})"
    else:
        filter_suffix = ""
    log(f"  URL        : {cfg['url']}{filter_suffix}")
    for elem in cfg["elements"]:
        log(f"  Element {elem['slot']}  : {elem['selector']}  (extract: {elem['extract']})")
    log(f"  Date range : {cfg['from_date'] or 'start'} -> {cfg['to_date'] or 'now'}")
    log(f"  Frequency  : {cfg['frequency']}  |  anchor: {cfg['sample_from']}  |  min gap: {gap_info}")
    log(f"  Format     : {sample_str}")
    log(f"  Threads    : {cfg['threads']}")
    log(f"  CSV layout : {cfg['csv_layout']}  |  result padding: {'yes' if cfg['result_padding'] else 'no'}")
    override_str = "yes" if cfg["file_override"] else "no"
    log(f"  Output     : {cfg['output']}  |  override: {override_str}")
    if cfg["fallback_candidates"] > 0:
        log(f"  Fallbacks  : {cfg['fallback_candidates']} candidate(s) per period")
    if cfg["reformat"]:
        pairs_str = "  ".join(f"{ls}->{vs}" for ls, vs in cfg["reformat_pairs"])
        merge_str = f"  |  merge: {cfg['label_case']}" if cfg["label_merge"] else ""
        log(f"  Reformat   : yes  |  pairs: {pairs_str}  |  sort: {cfg['sort']}{merge_str}")
    if cfg["split_output"] != "no":
        log(f"  Split out  : {cfg['split_output']}")
    fetch_mode = "headless Chromium" if cfg["headless_browser"] else "HTTP request"
    log(f"  Fetch mode : {fetch_mode}")
    log("=" * 60)
    if cfg["headless_browser"]:
        _check_playwright()

    _cdx_preflight(cfg)
    snapshots = get_snapshots(cfg)
    if not snapshots:
        sys.exit(_ERRORS["no_snapshots"])

    snapshots, fallbacks_map = sample_snapshots(snapshots, cfg)
    total = len(snapshots)
    results = [None] * total

    if cfg["headless_browser"]:
        log(f"[Setup]  Launching {cfg['threads']} Chromium instance(s) ...")
        _get_browser(cfg["threads"])
        atexit.register(_close_browser)
        log("[Setup]  Chromium ready.")

    try:
        failed_indices = run_pass(
            list(range(total)), snapshots, results, total, cfg,
            buffered=_GUI_MODE, fallbacks_map=fallbacks_map,
        )
        drain_buffer()
    finally:
        if cfg["headless_browser"]:
            _close_browser()

    if failed_indices and cfg["end_passes"] > 0:
        cfg_no_retry = {**cfg, "retries": 1}
        for pass_num in range(1, cfg["end_passes"] + 1):
            if not failed_indices:
                break
            log(f"\n[End pass {pass_num}/{cfg['end_passes']}]  Retrying {len(failed_indices)} failed snapshot(s) ...")
            time.sleep(cfg["delay"])
            failed_indices = run_pass(
                failed_indices, snapshots, results, total, cfg_no_retry,
                buffered=False, fallbacks_map=fallbacks_map,
            )
            drain_buffer()

    if failed_indices:
        if cfg["end_passes"] > 0:
            log(f"\n[Done]   {len(failed_indices)} snapshot(s) failed after exhausting all fallback candidates and end passes.")
        else:
            log(f"\n[Done]   {len(failed_indices)} snapshot(s) failed after exhausting all fallback candidates.")

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    log(f"[Done]   Finished in {mins}m {secs}s")

    # -- Write output ---------------------------------------------------------
    def _write_split(groups: dict):
        """Write one CSV (+ optional reformat) per group. groups = {suffix: [results]}"""
        log(f"\n[Split]  Writing {len(groups)} output file(s) ...")
        stem = os.path.splitext(os.path.basename(cfg["output"]))[0]
        ext  = os.path.splitext(cfg["output"])[1]
        for suffix, group_results in groups.items():
            raw_filename = f"{stem}_{suffix}{ext}"
            raw_path = os.path.join(cfg["raw_dir"], raw_filename)
            out_path = resolve_output_path(raw_path, cfg["file_override"])
            if out_path != raw_path:
                log(f"[Split]  '{raw_path}' already exists -- writing to '{out_path}' instead.")
            log(f"[Split]  '{suffix}' -> {len(group_results)} result(s) -> {out_path}")
            write_csv(group_results, cfg, out_path)
            if cfg["reformat"]:
                reformat_csv(group_results, cfg, out_path)

    def _write_merged(groups: dict):
        """Write all groups into a single merged CSV (+ optional reformat)."""
        log(f"\n[Merged]  Writing {len(groups)} group(s) into single file ...")
        raw_filename = os.path.basename(cfg["output"])
        raw_path = os.path.join(cfg["raw_dir"], raw_filename)
        out_path = resolve_output_path(raw_path, cfg["file_override"])
        if out_path != raw_path:
            log(f"[Merged]  '{raw_path}' already exists -- writing to '{out_path}' instead.")
        write_merged_csv(groups, cfg, out_path)
        if cfg["reformat"]:
            reformat_merged_csv(groups, cfg, out_path)

    def _split_by_url(result_list: list) -> dict:
        """Group a list of results by distinct original URL, preventing suffix collisions."""
        groups = {}
        seen_suffixes = {}

        for r in result_list:
            orig = r.get("original", "")
            if not orig:
                continue

            base_suffix = url_to_suffix(orig, cfg["url"])
            suffix = base_suffix

            counter = 2
            while suffix in seen_suffixes and seen_suffixes[suffix] != orig:
                suffix = f"{base_suffix}_{counter}"
                counter += 1

            seen_suffixes[suffix] = orig
            groups.setdefault(suffix, []).append(r)

        return groups

    if cfg["split_output"] != "no":
        dispatch = _write_split if cfg["split_output"] == "files" else _write_merged
        any_includes = [f for f in filters_any if not f["negate"]]
        all_includes = [f for f in filters_all if not f["negate"]]

        if not any_includes and not all_includes:
            # No include tokens at all - split by distinct URL
            groups = _split_by_url([r for r in results if r])
            if len(groups) <= 1:
                log(f"[Split]  Only one distinct URL found -- writing single output file.")
                raw_filename = os.path.basename(cfg["output"])
                out_path = resolve_output_path(os.path.join(cfg["raw_dir"], raw_filename), cfg["file_override"])
                write_csv(results, cfg, out_path)
                if cfg["reformat"]: reformat_csv(results, cfg, out_path)
            else:
                dispatch(groups)
            save_log(cfg["output"], cfg["base_dir"])

        else:
            groups = {}
            wildcard_bucket = []

            for f in any_includes:
                matched = [
                    r for r in results
                    if r and r.get("original") and _single_filter_matches(
                        r["original"], f["pattern"], f["mode"],
                        cfg["case_sensitive"], cfg["match_child_paths"]
                    )
                ]
                token_label = f["pattern"] or "*"
                if not matched:
                    log(f"[Split]  No results matched '{token_label}', skipping.")
                    continue

                if _is_wildcard_filter(f):
                    wildcard_bucket.extend(matched)
                else:
                    suffix = filter_to_suffix(f)
                    if suffix not in groups:
                        groups[suffix] = matched

            if any(_is_wildcard_filter(f) for f in all_includes):
                wildcard_bucket.extend([r for r in results if r and r.get("original")])

            if wildcard_bucket:
                seen_ids = set()
                deduped = []
                for m in wildcard_bucket:
                    if id(m) not in seen_ids:
                        seen_ids.add(id(m))
                        deduped.append(m)
                url_groups = _split_by_url(deduped)
                for u_suffix, u_results in url_groups.items():
                    final_suffix = u_suffix
                    counter = 2
                    while final_suffix in groups:
                        final_suffix = f"{u_suffix}_{counter}"
                        counter += 1
                    groups[final_suffix] = u_results

            if groups:
                dispatch(groups)
            else:
                log("[Split]  No groups produced -- writing single output file.")
                raw_filename = os.path.basename(cfg["output"])
                out_path = resolve_output_path(os.path.join(cfg["raw_dir"], raw_filename), cfg["file_override"])
                write_csv(results, cfg, out_path)
                if cfg["reformat"]: reformat_csv(results, cfg, out_path)

            save_log(cfg["output"], cfg["base_dir"])

    else:
        # Standard un-split output
        raw_filename = os.path.basename(cfg["output"])
        out_path = resolve_output_path(os.path.join(cfg["raw_dir"], raw_filename), cfg["file_override"])
        if out_path != os.path.join(cfg["raw_dir"], raw_filename):
            log(f"[CSV]    '{cfg['output']}' already exists -- writing to '{out_path}' instead.")
        write_csv(results, cfg, out_path)
        if cfg["reformat"]:
            reformat_csv(results, cfg, out_path)
        save_log(cfg["output"], cfg["base_dir"])




if __name__ == "__main__":
    main()

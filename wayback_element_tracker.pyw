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

# Playwright is optional — imported lazily when headless_browser = yes
_playwright_available = None  # None = not yet checked

# ── Constants ────────────────────────────────────────────────────────────────
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

MAX_ELEMENTS = 5


# ── URL Filter Helpers ────────────────────────────────────────────────────────
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

    # ── filter_any (OR) ───────────────────────────────────────────────────────
    any_includes = [f for f in filters_any if not f["negate"]]
    any_excludes = [f for f in filters_any if f["negate"]]

    for f in any_excludes:
        if _single_filter_matches(url, f["pattern"], f["mode"], cs, isp):
            return False

    if any_includes:
        if not any(_single_filter_matches(url, f["pattern"], f["mode"], cs, isp)
                   for f in any_includes):
            return False

    # ── filter_all (AND) ──────────────────────────────────────────────────────
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

# ── Logging & Sequential Print Buffer ────────────────────────────────────────
_log_lines = []
_print_lock = threading.Lock()
_print_buffer = {}  # index -> message string
_next_to_print = [1]  # list so threads share the same mutable object


def log(msg: str=""):
    """Print immediately and record in log."""
    with _print_lock:
        print(msg)
        _log_lines.append(msg)


def buffer_and_flush(index: int, msg: str):
    """
    Buffer a snapshot result by its 1-based index, then flush all
    consecutive buffered messages so output appears in order.
    """
    with _print_lock:
        _print_buffer[index] = msg
        while _next_to_print[0] in _print_buffer:
            m = _print_buffer.pop(_next_to_print[0])
            print(m)
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
            print(m)
            _log_lines.append(m)
            _next_to_print[0] += 1


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


# ── Attribute Helpers ─────────────────────────────────────────────────────────
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


# ── HTML Element Parser ───────────────────────────────────────────────────────
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

    Nth direct child(ren) — bare integers after a tag (no following tag):
        element_1 = <div class="paragraph1"> 2 3
        -> the 3rd child element of the 2nd child element of div.paragraph1
    """
    raw = raw.strip()

    # Find all opening HTML tags (exclude closing tags via negative lookahead for /)
    opening_tag_re = re.compile(r'<(?!/)[^>]+>')
    tag_matches = list(opening_tag_re.finditer(raw))

    if not tag_matches:
        sys.exit(
            f"[Error] Could not parse element_{slot} in settings.txt.\n"
            f"        Paste the full HTML tag, e.g.:\n"
            f"        element_{slot} = <p class=\"rbx-lead\" title=\"28,760,666\">28M+</p>"
        )

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
            sys.exit(
                f"[Error] Could not parse {label} in settings.txt.\n"
                f"        Paste the full HTML tag, e.g.:\n"
                f"        element_{slot} = <p class=\"rbx-lead\" title=\"28,760,666\">28M+</p>"
            )
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

    # First tag — never has a preceding nth number
    first_sel, first_attrs = tag_to_selector(tag_matches[0].group(), 0)
    steps.append({"sel": first_sel, "nth": None})
    extractables = first_attrs

    # Subsequent opening tags — inspect the gap before each for an nth number
    for i in range(1, len(tag_matches)):
        gap = raw[tag_matches[i - 1].end(): tag_matches[i].start()]
        gap_text = re.sub(r'<[^>]+>', '', gap)      # strip any closing tags in gap
        nums = re.findall(r'\b(\d+)\b', gap_text)
        nth = int(nums[-1]) if nums else None

        sel, attrs = tag_to_selector(tag_matches[i].group(), i)
        steps.append({"sel": sel, "nth": nth})
        extractables = attrs

    # Suffix after the last opening tag — bare integers become nth-child steps,
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


# ── Date / Time Formatting ────────────────────────────────────────────────────
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


# ── Anchor Point Calculation ──────────────────────────────────────────────────
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

element_2 = 
extract_2 = 

element_3 = 
extract_3 = 

element_4 = 
extract_4 = 

element_5 = 
extract_5 = 

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

# --- FETCH MODE ------------------------------------------------------------------------------------------
headless_browser = no

# --- ADVANCED --------------------------------------------------------------------------------------------
min_gap = 0.5
delay = 10
retries = 5
fallback_candidates = 1
threads = 3
"""


def load_settings(path="settings.txt") -> dict:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_SETTINGS)
        abs_path = os.path.abspath(path)
        print(f"[Setup]  settings.txt not found -- created a blank one at: {abs_path}")
        print(f"[Setup]  Fill in your url and element_1, then run the program again.")
        sys.exit(0)

    # Seed raw defaults by parsing DEFAULT_SETTINGS — single source of truth.
    raw = {}
    for line in DEFAULT_SETTINGS.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        raw[k.strip().lower()] = v.strip()
    # extract_N defaults to "text" when blank in DEFAULT_SETTINGS
    for i in range(1, MAX_ELEMENTS + 1):
        if not raw.get(f"extract_{i}"):
            raw[f"extract_{i}"] = "text"

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
            if key in raw and value:
                raw[key] = value

    if not raw["url"]:
        sys.exit("[Error] 'url' is missing from settings.txt")

    # Strip trailing * from URL — treat it the same as filter_any = *
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
        sys.exit(f"[Error] 'frequency' must be one of: {', '.join(FREQ_MAP)}")
    if raw["sample_from"].lower() not in ("start", "middle", "end"):
        sys.exit("[Error] 'sample_from' must be 'start', 'middle', or 'end'")
    if raw["csv_layout"].lower() not in ("columns", "rows"):
        sys.exit("[Error] 'csv_layout' must be 'columns' or 'rows'")
    if raw["year_digits"] not in ("2", "4"):
        sys.exit("[Error] 'year_digits' must be '2' or '4'")
    for field in ("from_date", "to_date"):
        val = raw[field]
        if val and (not val.isdigit() or len(val) != 8):
            sys.exit(f"[Error] '{field}' must be in YYYYMMDD format (e.g. 20231105), got: {val!r}")

    try:
        min_gap_frac = float(raw["min_gap"])
        if min_gap_frac < 0:
            raise ValueError
    except ValueError:
        sys.exit("[Error] 'min_gap' must be a number >= 0, e.g. 0.5")

    try:
        fallback_candidates = int(raw["fallback_candidates"])
        if fallback_candidates < 0:
            raise ValueError
    except ValueError:
        sys.exit("[Error] 'fallback_candidates' must be an integer >= 0")

    min_gap_secs = int(FREQ_SECONDS.get(raw["frequency"], 0) * min_gap_frac)

    cdx_any, filters_any = parse_filters(raw["filter_any"])
    cdx_all, filters_all = parse_filters(raw["filter_all"])
    cdx_wildcard = cdx_any or cdx_all or (raw["_url_wildcard"] == "yes")

    # ── Reformat validation ───────────────────────────────────────────────────
    do_reformat = yesno(raw["reformat"])
    reformat_pairs = []
    sort = raw["sort"].strip().lower()

    if do_reformat:
        if sort not in ("alphabet", "reverse", "unsorted"):
            sys.exit("[Error] 'sort' must be 'alphabet', 'reverse', or 'unsorted'.")
        rl = raw["label_elements"].strip()
        rv = raw["value_elements"].strip()
        if not rl or not rv:
            sys.exit(
                "[Error] 'label_elements' and 'value_elements' must both be "
                "set when reformat = yes.\n"
                "        Use space-separated slot numbers for multiple pairs, e.g.:\n"
                "        label_elements = 1 2\n"
                "        value_elements = 3 4"
            )
        try:
            label_slots = [int(x) for x in rl.split()]
            value_slots = [int(x) for x in rv.split()]
        except ValueError:
            sys.exit("[Error] 'label_elements' and 'value_elements' must be "
                     "integers matching element_N slot numbers.")
        if len(label_slots) != len(value_slots):
            sys.exit(
                f"[Error] 'label_elements' has {len(label_slots)} slot(s) but "
                f"'value_elements' has {len(value_slots)}. They must have the same count."
            )
        for ls, vs in zip(label_slots, value_slots):
            if ls == vs:
                sys.exit(f"[Error] label_elements and value_elements must be different "
                         f"(slot {ls} appears in both).")
        reformat_pairs = list(zip(label_slots, value_slots))

    elements = []
    for i in range(1, MAX_ELEMENTS + 1):
        html = raw[f"element_{i}"]
        if not html:
            continue
        extract = raw[f"extract_{i}"].lower()
        if extract != "text" and not extract.startswith("data-") \
                and extract not in KNOWN_ATTRS:
            sys.exit(
                f"[Error] extract_{i} = '{extract}' is not recognised.\n"
                f"        Use 'text', a known attribute, or a data-* attribute."
            )
        selector_chain, extractables = parse_element_html(html, i)

        def _step_display(step):
            if step["sel"] is None:
                return f"[child {step['nth']}]"
            elif step["nth"] is not None:
                return f"{step['sel']} [{step['nth']}]"
            else:
                return step["sel"]

        selector = " > ".join(_step_display(s) for s in selector_chain)
        if extract != "text" and extract not in extractables:
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
        sys.exit("[Error] At least one element_1 through element_5 must be set.")

    try:
        threads = int(raw["threads"])
        if threads < 1:
            raise ValueError
    except ValueError:
        sys.exit("[Error] 'threads' must be a positive integer.")

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
        "fallback_candidates": fallback_candidates,
        "threads": threads,
        "headless_browser": yesno(raw["headless_browser"]),
        "reformat": do_reformat,
        "reformat_pairs": reformat_pairs,
        "sort": sort,
        "zero_fill": raw["zero_fill"].strip().lower(),
        "fill_first": yesno(raw["fill_first"]),
        "split_output": raw["split_output"].strip().lower(),
        "merged_meta": raw["merged_meta"].strip().lower(),
        "match_child_paths": yesno(raw["match_child_paths"]),
        "collision_priority": "",  # set after validation below
    }

    if cfg["zero_fill"] not in ("no", "adjacent", "snapshot"):
        sys.exit("[Error] 'zero_fill' must be 'no', 'adjacent', or 'snapshot'.")

    if cfg["split_output"] not in ("no", "files", "merged"):
        sys.exit("[Error] 'split_output' must be 'no', 'files', or 'merged'.")

    if cfg["merged_meta"] not in ("interleaved", "grouped"):
        sys.exit("[Error] 'merged_meta' must be 'interleaved' or 'grouped'.")

    # ── collision_priority validation ───────────────────────────────────────────
    collision_priority = raw["collision_priority"].strip().lower()
    if collision_priority not in ("time", "filter"):
        sys.exit("[Error] 'collision_priority' must be 'time' or 'filter'.")
    cfg["collision_priority"] = collision_priority

    return cfg


# ── Step 1: Get Snapshot List ─────────────────────────────────────────────────
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

            # ── Distinct-URL sanity check ─────────────────────────────────
            distinct_urls = list(dict.fromkeys(s["original"] for s in snapshots))
            n_distinct = len(distinct_urls)
            WARN_THRESHOLD = 20
            if n_distinct > WARN_THRESHOLD:
                log(f"\n[Warning] Your filter matched {n_distinct} distinct URLs.")
                log(f"          This may be unintentionally broad (e.g. tracking every")
                log(f"          sub-ID under a path like /page/1, /page/2, ...).")
                preview = distinct_urls[:5]
                for u in preview:
                    log(f"            {u}")
                if n_distinct > 5:
                    log(f"            ... and {n_distinct - 5} more")
                log(f"")
                try:
                    answer = input("  Continue anyway? [Y/N]: ").strip().lower()
                except EOFError:
                    answer = "n"
                if answer not in ("y", "yes"):
                    sys.exit("[Aborted] Re-check your filter settings and try again.")
                log(f"")

            return snapshots
        except Exception as e:
            clean_err = re.sub(r'https?://\S+', '', str(e)).strip().strip(":")
            if attempt < cfg["retries"]:
                log(f"[CDX]    Query failed: {clean_err} -- retrying in {cfg['delay']}s ...")
                time.sleep(cfg["delay"])
            else:
                sys.exit(f"[CDX]    Query failed after {cfg['retries']} attempts: {clean_err}")


# ── Step 2: Sampling ──────────────────────────────────────────────────────────
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


# ── Step 3: Fetch One Snapshot ────────────────────────────────────────────────
def _check_playwright():
    """Exit with a helpful message if the playwright package is not installed."""
    global _playwright_available
    if _playwright_available is not None:
        return
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        _playwright_available = True
    except ImportError:
        sys.exit(
            "[Error] 'headless_browser = yes' requires Playwright.\n"
            "        Install it with:\n"
            "          pip install playwright"
        )


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
                # networkidle. Both timeouts are soft — we proceed regardless.
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
        sys.exit(f"[Error]  Failed to launch Chromium: {clean_err}")


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

    emit = buffer_and_flush if buffered else lambda idx, m: log(m)
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

                for elem in cfg["elements"]:
                    sel_chain = elem["selector_chain"]
                    extract = elem["extract"]
                    sel_display = elem["selector"]
                    label = f"  {sel_display:<{max_sel_len}}"

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
                        elem_values[elem["slot"]] = []
                        lines.append(f"{label}: (no element)")
                    else:
                        values = [v for m in matches
                                  for v in [extract_value(m, extract)] if v]
                        if values:
                            elem_values[elem["slot"]] = values
                            lines.append(f"{label}: {', '.join(values)}")
                        else:
                            elem_values[elem["slot"]] = []
                            lines.append(f"{label}: (blank)")

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

    # All candidates failed — report using the primary snapshot's info
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


# ── Result Padding ────────────────────────────────────────────────────────────
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


# ── Output Path Resolution ────────────────────────────────────────────────────
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


# ── Output Folder Structure ───────────────────────────────────────────────────
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


# ── Step 4: Write CSV ─────────────────────────────────────────────────────────
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

                descriptors.append((f"url ({suffix})",   lambda r, _=None: [r["url"]]))
                descriptors.append((f"error ({suffix})", lambda r, _=None: [r["error"]]))

                # Group label row then column headers
                writer.writerow([suffix] + [""] * (len(descriptors) - 1))
                writer.writerow([label for label, _ in descriptors])
                for r in g_results:
                    writer.writerow([fn(r)[0] for _, fn in descriptors])

    total = sum(len(g) for _, g in padded_groups)
    log(f"\n[Merged]  Saved {total} total snapshots ({len(padded_groups)} group(s))"
        f" -> {os.path.abspath(output_path)}")


def reformat_merged_csv(groups: dict, cfg: dict, output_path: str) -> None:
    """
    Write a merged reformatted CSV for all groups into one file.

    rows layout:
        Shared date/time header.  Then, depending on merged_meta:
          interleaved : for each group — group-label row, url row, error row,
                        then that group's data rows — all in one pass.
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

    def build_group_data(g_results):
        g_results = apply_padding(g_results, cfg)
        snap_dates  = [r["date"]  if r else "" for r in g_results]
        snap_times  = [r["time"]  if r else "" for r in g_results]
        snap_urls   = [r["url"]   if r else "" for r in g_results]
        snap_errors = [r["error"] if r else "" for r in g_results]

        pair_data = []
        for label_slot, value_slot in pairs:
            seen_labels, seen_set = [], set()
            for r in g_results:
                if not r: continue
                for lbl in r["elem_values"].get(label_slot, []):
                    if lbl and lbl not in seen_set:
                        seen_labels.append(lbl); seen_set.add(lbl)
            if sort_mode == "alphabet":
                ordered = sorted(seen_labels, key=lambda x: x.lower())
            elif sort_mode == "reverse":
                ordered = sorted(seen_labels, key=lambda x: x.lower(), reverse=True)
            else:
                ordered = seen_labels
            snap_maps = []
            for r in g_results:
                if not r: snap_maps.append({}); continue
                lbls = r["elem_values"].get(label_slot, [])
                vals = r["elem_values"].get(value_slot, [])
                snap_maps.append({lbls[i]: vals[i] if i < len(vals) else ""
                                  for i in range(len(lbls))})
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

    descriptors.append(("url", lambda r, _=None: [r["url"]]))
    descriptors.append(("error", lambda r, _=None: [r["error"]]))

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


# ── Step 5: Reformat CSV ──────────────────────────────────────────────────────
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

    # ── Build ordered labels and snap_maps for each pair ─────────────────────
    def build_pair(label_slot, value_slot):
        all_labels = [v for r in results if r for v in r["elem_values"].get(label_slot, [])]
        all_values = [v for r in results if r for v in r["elem_values"].get(value_slot, [])]
        if len(set(all_labels)) <= 1:
            log(f"[Reformat] Skipping pair ({label_slot}->{value_slot}): "
                f"label element has only one unique value across all snapshots.")
            return None, None
        if len(set(all_values)) <= 1:
            log(f"[Reformat] Skipping pair ({label_slot}->{value_slot}): "
                f"value element has only one unique value across all snapshots.")
            return None, None

        seen_labels: list = []
        seen_set: set = set()
        for r in results:
            if not r:
                continue
            for lbl in r["elem_values"].get(label_slot, []):
                if lbl and lbl not in seen_set:
                    seen_labels.append(lbl)
                    seen_set.add(lbl)

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
            lbls = r["elem_values"].get(label_slot, [])
            vals = r["elem_values"].get(value_slot, [])
            snap_maps.append({lbls[i]: vals[i] if i < len(vals) else ""
                              for i in range(len(lbls))})
        return ordered_labels, snap_maps

    pair_data = []
    for label_slot, value_slot in pairs:
        ordered_labels, snap_maps = build_pair(label_slot, value_slot)
        if ordered_labels is not None:
            pair_data.append((ordered_labels, snap_maps))

    if not pair_data:
        return

    # ── Snapshot header values (dates/times) ─────────────────────────────────
    snap_dates  = [r["date"]  if r else "" for r in results]
    snap_times  = [r["time"]  if r else "" for r in results]
    snap_urls   = [r["url"]   if r else "" for r in results]
    snap_errors = [r["error"] if r else "" for r in results]

    # ── Zero-fill pre-processing ──────────────────────────────────────────────
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

    # ── Write output ──────────────────────────────────────────────────────────
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


# ── Run One Pass Over Snapshot Indices ────────────────────────────────────────
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


# ── Filter / URL → Filename Suffix ───────────────────────────────────────────
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


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start_time = time.time()
    cfg = load_settings()

    filters_any = cfg["filters_any"]
    filters_all = cfg["filters_all"]

    # ── Set up output folder structure ────────────────────────────────────────
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
    log("  Wayback Element Tracker v2.0.0")
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
        log(f"  Reformat   : yes  |  pairs: {pairs_str}  |  sort: {cfg['sort']}")
    if cfg["split_output"] != "no":
        log(f"  Split out  : {cfg['split_output']}")
    fetch_mode = "headless Chromium" if cfg["headless_browser"] else "HTTP request"
    log(f"  Fetch mode : {fetch_mode}")
    log("=" * 60)
    if cfg["headless_browser"]:
        _check_playwright()

    snapshots = get_snapshots(cfg)
    if not snapshots:
        sys.exit("No snapshots to process.")

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
            buffered=True, fallbacks_map=fallbacks_map,
        )
        drain_buffer()
    finally:
        if cfg["headless_browser"]:
            _close_browser()

    if failed_indices:
        log(f"\n[Done]   {len(failed_indices)} snapshot(s) failed after exhausting all fallback candidates.")

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    log(f"[Done]   Finished in {mins}m {secs}s")

    # ── Write output ─────────────────────────────────────────────────────────
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
            # No include tokens at all — split by distinct URL
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



# ── GUI ───────────────────────────────────────────────────────────────────────
import tkinter as tk
import tkinter.font as _tkfont
from tkinter import ttk, scrolledtext, messagebox
import queue as _queue
import io as _io
import subprocess
import ctypes as _ctypes

# Enable DPI awareness before any Tk window is created so controls render
# sharply on high-DPI displays.  Requires Windows 8.1+ (shcore).
if sys.platform == "win32":
    try:
        _ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

SETTINGS_PATH = "settings.txt"
YES_NO = ["yes", "no"]

# ── Tooltip texts (sourced from README) ───────────────────────────────────────
_TIPS = {
    "url": (
        "The full URL of the page to track.\n"
        "e.g.  https://www.example.com"
    ),
    "filter_any": (
        "URL must match at least ONE filter, or will be skipped.\n\n"
        "(blank)      \u2192  match only the exact URL, no variants\n"
        "*            \u2192  include all URL variants\n"
        "/subpage     \u2192  match URLs where /subpage appears at the end of the path\n"
        "key=value    \u2192  match only URLs containing key=value as a query parameter\n"
        "[filter]*    \u2192  substring match anywhere in the URL\n"
        "                  e.g. key=* matches both key=1 and key=2\n"
        "                       /subpage* matches /subpage-a and /subpage-b\n"
        "![filter]    \u2192  exclude instead of include\n"
        "                  (works with all of the above)\n\n"
        "Multiple filters are separated by spaces.\n"
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
        "yes  \u2192  /subpage also matches /subpage/child, /subpage/child/page, etc.\n"
        "no   \u2192  /subpage matches only example.com/subpage exactly\n\n"
        "Note: substring filters like /subpage* always match child paths\n"
        "regardless of this setting."
    ),
    "element": (
        "The HTML element to track.\n"
        "Paste the HTML tag from Inspect Element.\n\n"
        "A parent element can be entered before the target element,\n"
        "separated by spaces:\n"
        "  <div class=\"row\"> <span class=\"value\">5%</span>\n\n"
        "To target a specific occurrence, place a number directly before\n"
        "the child element:\n"
        "  <div class=\"row\"> 2<span class=\"value\">text</span>\n"
        "  \u2192 the 2nd span.value inside div.row\n\n"
        "Bare numbers after a parent select by child position:\n"
        "  <div class=\"row\"> 2 3\n"
        "  \u2192 the 3rd child of the 2nd child of div.row\n\n"
        "All of these can be combined and stacked freely."
    ),
    "extract": (
        "What to extract from the element:\n\n"
        "text         \u2192  visible text inside the element\n"
        "title        \u2192  title=\"...\" attribute\n"
        "href         \u2192  href=\"...\" attribute (links)\n"
        "src          \u2192  src=\"...\" attribute (images, scripts)\n"
        "value        \u2192  value=\"...\" attribute (inputs)\n"
        "content      \u2192  content=\"...\" attribute (meta tags)\n"
        "alt          \u2192  alt=\"...\" attribute (image descriptions)\n"
        "placeholder  \u2192  placeholder=\"...\" attribute (input hints)\n"
        "datetime     \u2192  datetime=\"...\" attribute (time elements)\n"
        "action       \u2192  action=\"...\" attribute (forms)\n"
        "data-*       \u2192  any custom data attribute\n"
        "                  e.g. data-count, data-value\n\n"
        "If a selector matches multiple elements on the page, all of them\n"
        "are captured as separately numbered columns or rows."
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
        "all      \u2192  every available snapshot\n"
        "hourly   \u2192  one snapshot per hour\n"
        "daily    \u2192  one snapshot per day\n"
        "weekly   \u2192  one snapshot per week\n"
        "monthly  \u2192  one snapshot per month\n"
        "yearly   \u2192  one snapshot per year"
    ),
    "sample_from": (
        "Which snapshot to pick within each frequency period:\n\n"
        "start   \u2192  the snapshot closest to the START of the period\n"
        "middle  \u2192  the snapshot closest to the MIDDLE of the period\n"
        "end     \u2192  the snapshot closest to the END of the period\n\n"
        "Has no effect when frequency = all."
    ),
    "collision_priority": (
        "When multiple URL variants have snapshots in the same time period,\n"
        "determines which one is preferred:\n\n"
        "time    \u2192  the variant whose timestamp is closest to the\n"
        "             sample_from anchor wins\n"
        "filter  \u2192  earlier listed filter_any filters take priority\n"
        "             over later ones\n\n"
        "Has no effect when no URL variants are tracked,\n"
        "or when split_output = files."
    ),
    "convention": (
        "us        \u2192  month first  (November 5, 2023)\n"
        "european  \u2192  day first    (5 November 2023)"
    ),
    "date_style": (
        "long     \u2192  November 5, 2023  /  5 November 2023\n"
        "short    \u2192  Nov 5, 2023  /  5 Nov 2023\n"
        "numeric  \u2192  11/5/2023  /  5/11/2023"
    ),
    "year_digits": (
        "4  \u2192  2023\n"
        "2  \u2192  23"
    ),
    "date_padding": (
        "yes  \u2192  11/05/2023\n"
        "no   \u2192  11/5/2023"
    ),
    "time_format": (
        "12h  \u2192  2:30 PM\n"
        "24h  \u2192  14:30"
    ),
    "time_padding": (
        "yes  \u2192  06:50\n"
        "no   \u2192  6:50"
    ),
    "show_seconds": "Show seconds in the time?",
    "show_month": (
        "Show the month in the output CSV?\n\n"
        "At least one of show_month, show_day, show_year,\n"
        "or show_time must be yes."
    ),
    "show_day": (
        "Show the day in the output CSV?\n\n"
        "At least one of show_month, show_day, show_year,\n"
        "or show_time must be yes."
    ),
    "show_year": (
        "Show the year in the output CSV?\n\n"
        "At least one of show_month, show_day, show_year,\n"
        "or show_time must be yes."
    ),
    "show_time": (
        "Show the time in the output CSV?\n\n"
        "At least one of show_month, show_day, show_year,\n"
        "or show_time must be yes."
    ),
    "output": (
        "CSV file name.\n\n"
        "After each run, a .log file is saved alongside the CSV with\n"
        "the same base name. Each new program execution is added to the\n"
        "end of the log file, which will track the last 10 runs."
    ),
    "file_override": (
        "Whether to overwrite the output file if it already exists.\n\n"
        "yes  \u2192  overwrite\n"
        "no   \u2192  add an incrementing counter instead\n"
        "          e.g.  wayback_results.csv\n"
        "                wayback_results_1.csv\n"
        "                wayback_results_2.csv  \u2026"
    ),
    "csv_layout": (
        "columns  \u2192  each attribute is a column, each snapshot is a row\n"
        "               date | time | element | url | error\n\n"
        "rows     \u2192  each attribute is a row, each snapshot is a column\n"
        "               date | Jan 1 | Feb 1 | \u2026\n"
        "               elem | value | value | \u2026\n\n"
        "The url column contains the full Wayback Machine URL of each snapshot.\n"
        "The error column is blank on success, or contains the failure reason\n"
        "(e.g. timeout, HTTP 404).\n\n"
        "When an element cannot be extracted, its cell is left empty.\n"
        "The console output distinguishes two cases:\n"
        "  (no element)  \u2192  element was not found anywhere on the page\n"
        "  (blank)       \u2192  element was found but the extracted value was empty"
    ),
    "result_padding": (
        "Insert blank rows/columns for time periods with no archived snapshots.\n\n"
        "yes  \u2192  Jan 1 | Feb 1 | Mar 1 | \u2026\n"
        "          value |        | value | \u2026\n\n"
        "no   \u2192  Jan 1 | Mar 1 | \u2026\n"
        "          value | value | \u2026\n\n"
        "Has no effect when frequency = all."
    ),
    "split_output": (
        "no      \u2192  all variants written into one file;\n"
        "             collisions resolved by collision_priority\n"
        "files   \u2192  one output file per URL variant or filter\n"
        "merged  \u2192  one output file containing all filter groups separately"
    ),
    "reformat": (
        "Writes an additional [filename]_reformatted CSV per raw output file.\n\n"
        "Pairs two elements \u2014 one as a label and one as a value for that label.\n"
        "Moves each value element into one row (or column) per unique label.\n\n"
        "e.g.\n"
        "  date | Jan 1 | Feb 1 | \u2026        date  | Jan 1 | Feb 1 | \u2026\n"
        "  elem | label | label | \u2026   \u2192   label | value | value | \u2026\n"
        "  elem | value | value | \u2026"
    ),
    "label_elements": (
        "The index of the element(s) whose output become the LABELS\n"
        "in the reformatted file.\n\n"
        "e.g.  2  will treat element_2 as the label.\n\n"
        "Multiple indexes separated by spaces.\n"
        "The index of the label element is paired with the index of\n"
        "the value element at the same position."
    ),
    "value_elements": (
        "The index of the element(s) whose output become the VALUES\n"
        "in the reformatted file.\n\n"
        "e.g.  3  will treat element_3 as the value to track.\n\n"
        "Multiple indexes separated by spaces.\n"
        "For label_elements = 1 2 and value_elements = 3 4,\n"
        "elements 1 and 3 are paired and elements 2 and 4 are paired."
    ),
    "sort": (
        "How to order the label rows / columns in the reformatted file:\n\n"
        "unsorted  \u2192  labels appear in first-seen order\n"
        "alphabet  \u2192  alphabetical A\u2013Z (case insensitive)\n"
        "reverse   \u2192  alphabetical Z\u2013A (case insensitive)"
    ),
    "zero_fill": (
        "When a label first appears partway through the timeline,\n"
        "places a 0 before its first value.\n\n"
        "no        \u2192  disabled\n"
        "adjacent  \u2192  places 0 in the cell directly before the first value\n"
        "snapshot  \u2192  places 0 in the snapshot before the first value\n"
        "               (only effective when result_padding is enabled)"
    ),
    "fill_first": (
        "Also place a 0 before labels whose first value appears\n"
        "at the very start of the timeline."
    ),
    "merged_meta": (
        "Controls where snapshot URLs and errors appear in the reformatted\n"
        "file when split_output = merged. Has no effect otherwise.\n\n"
        "grouped      \u2192  all data rows for all groups appear first,\n"
        "                  then all url rows, then all error rows at the bottom\n"
        "interleaved  \u2192  each filter has a group label, then url, then error,\n"
        "                  then that filter's data rows"
    ),
    "headless_browser": (
        "Use a headless Chromium browser to fetch every snapshot instead\n"
        "of a plain HTTP request.\n\n"
        "Enable this when the regular fetch consistently returns blank or\n"
        "missing values that are visible when loading the page in a real\n"
        "browser. This executes each page's JavaScript fully before\n"
        "extracting elements, which is needed when a site populates\n"
        "element content with JavaScript.\n\n"
        "Note: significantly slower and resource intensive than the default\n"
        "method. Chromium (~300 MB) is downloaded automatically on first use."
    ),
    "min_gap": (
        "Minimum gap between 2 consecutive selected snapshots, as a\n"
        "fraction of the frequency period. Snapshots closer together\n"
        "than this are compared and the one farther from its anchor\n"
        "is discarded.\n\n"
        "0.5  \u2192  half the period\n"
        "          e.g. ~15 days for monthly, 12 hours for daily\n"
        "0    \u2192  disabled\n\n"
        "Has no effect when frequency = all."
    ),
    "delay": (
        "Seconds to wait between retry attempts\n"
        "and between CDX query retries."
    ),
    "retries": (
        "How many times to retry a failing snapshot or CDX query\n"
        "before giving up.\n\n"
        "Note: HTTP 404 and 403 responses are not retried,\n"
        "they fail immediately."
    ),
    "fallback_candidates": (
        "When a snapshot fails, how many closest snapshots from the\n"
        "same time period to try before giving up.\n\n"
        "Has no effect when frequency = all."
    ),
    "threads": (
        "Number of parallel threads for fetching snapshots.\n\n"
        "Has no effect when headless_browser = yes."
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
        lbl = tk.Label(tw, text=self._text, justify="left",
                       background="#ffffff", foreground="#000000",
                       relief="flat", borderwidth=0,
                       font=("Segoe UI", 9),
                       wraplength=380, padx=7, pady=4)
        lbl.pack(padx=1, pady=1)
        tw.update_idletasks()
        sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
        if self._follow_cursor and event is not None:
            # Position just below and to the right of the cursor
            x = event.x_root + 16
            y = event.y_root + 16
        else:
            # Position to the right of the widget
            x = self._widget.winfo_rootx() + self._widget.winfo_width() + 6
            y = self._widget.winfo_rooty()
        if x + tw.winfo_width() > sw:
            x -= tw.winfo_width() + (32 if self._follow_cursor else 12)
        if y + tw.winfo_height() > sh:
            y -= tw.winfo_height() + (32 if self._follow_cursor else 0)
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
        self.root.title("Wayback Element Tracker")
        self.root.minsize(700, 640)

        # ── Modern Windows look ───────────────────────────────────────────────
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
        for key in ("frequency", "headless_browser", "reformat", "split_output"):
            self._var(key).trace_add("write", self._update_states)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log()

    # ── Variable helpers ──────────────────────────────────────────────────────
    def _var(self, key, default=""):
        if key not in self._vars:
            sv = tk.StringVar(value=default)
            sv.trace_add("write", self._mark_dirty)
            self._vars[key] = sv
        return self._vars[key]

    def _mark_dirty(self, *_):
        if not self._loading:
            self._unsaved = True

    # ── Layout helpers ────────────────────────────────────────────────────────
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
        _Tooltip(btn, tip)
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
        return e

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
        return ttk.Combobox(parent, textvariable=self._var(key), values=values,
                            state="normal" if editable else "readonly", width=width)

    # ── Tab builders ──────────────────────────────────────────────────────────
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
            cp_reason = "frequency = all  and  split_output = files"
        elif freq_all:
            cp_reason = "frequency = all"
        else:
            cp_reason = "split_output = files"
        self._set_state("collision_priority", freq_all or split_files, cp_reason)

        if no_reformat and not_merged:
            mm_reason = "reformat = no  and  split_output \u2260 merged"
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
        self._field(f, r, "url",
                    lambda p: self._entry(p, "url", 48),
                    tip_key="url"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Filters"); r += 1
        self._field(f, r, "filter_any",
                    lambda p: self._entry(p, "filter_any", 48),
                    tip_key="filter_any"); r += 1
        self._field(f, r, "filter_all",
                    lambda p: self._entry(p, "filter_all", 48),
                    tip_key="filter_all"); r += 1
        self._field(f, r, "case_sensitive",
                    lambda p: self._combo(p, "case_sensitive", YES_NO),
                    tip_key="case_sensitive"); r += 1
        self._field(f, r, "match_child_paths",
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

            ttk.Label(f, text="element").grid(
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
                          padx=3, pady=2)
            txt.grid(row=r, column=1, sticky="ew", padx=(0, 4), pady=3)
            self._element_text_widgets[f"element_{i}"] = txt

            def _on_text_modified(e, _txt=txt):
                if _txt.edit_modified() and not self._loading:
                    self._unsaved = True
                _txt.edit_modified(False)
            txt.bind("<<Modified>>", _on_text_modified)
            self._qbtn(f, r, "element"); r += 1

            ttk.Label(f, text="extract").grid(
                row=r, column=0, sticky="w", padx=(10, 4), pady=3)
            ttk.Combobox(f, textvariable=self._var(f"extract_{i}"),
                         values=EXTRACT_OPTS, state="normal", width=18,
                         height=EXTRACT_HEIGHT).grid(
                row=r, column=1, sticky="w", padx=(0, 4), pady=3)
            self._qbtn(f, r, "extract"); r += 1

    def _build_schedule_tab(self):
        f = self._scrollable_tab("Schedule")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Date Range"); r += 1
        self._field(f, r, "from_date",
                    lambda p: self._entry(p, "from_date", 12),
                    tip_key="from_date"); r += 1
        self._field(f, r, "to_date",
                    lambda p: self._entry(p, "to_date", 12),
                    tip_key="to_date"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Snapshot Frequency"); r += 1
        self._field(f, r, "frequency",
                    lambda p: self._combo(p, "frequency",
                    ["all", "hourly", "daily", "weekly", "monthly", "yearly"]),
                    tip_key="frequency"); r += 1
        self._field(f, r, "sample_from",
                    lambda p: self._combo(p, "sample_from", ["start", "middle", "end"]),
                    tip_key="sample_from"); r += 1
        self._field(f, r, "collision_priority",
                    lambda p: self._combo(p, "collision_priority", ["time", "filter"]),
                    tip_key="collision_priority"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Date & Time Format"); r += 1
        self._field(f, r, "convention",
                    lambda p: self._combo(p, "convention", ["us", "european"]),
                    tip_key="convention"); r += 1
        self._field(f, r, "date_style",
                    lambda p: self._combo(p, "date_style", ["long", "short", "numeric"]),
                    tip_key="date_style"); r += 1
        self._field(f, r, "year_digits",
                    lambda p: self._combo(p, "year_digits", ["4", "2"]),
                    tip_key="year_digits"); r += 1
        self._field(f, r, "date_padding",
                    lambda p: self._combo(p, "date_padding", YES_NO),
                    tip_key="date_padding"); r += 1
        self._field(f, r, "time_format",
                    lambda p: self._combo(p, "time_format", ["12h", "24h"]),
                    tip_key="time_format"); r += 1
        self._field(f, r, "time_padding",
                    lambda p: self._combo(p, "time_padding", YES_NO),
                    tip_key="time_padding"); r += 1
        self._field(f, r, "show_seconds",
                    lambda p: self._combo(p, "show_seconds", YES_NO),
                    tip_key="show_seconds"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Show in CSV"); r += 1
        self._field(f, r, "show_month",
                    lambda p: self._combo(p, "show_month", YES_NO),
                    tip_key="show_month"); r += 1
        self._field(f, r, "show_day",
                    lambda p: self._combo(p, "show_day", YES_NO),
                    tip_key="show_day"); r += 1
        self._field(f, r, "show_year",
                    lambda p: self._combo(p, "show_year", YES_NO),
                    tip_key="show_year"); r += 1
        self._field(f, r, "show_time",
                    lambda p: self._combo(p, "show_time", YES_NO),
                    tip_key="show_time"); r += 1

    def _build_output_tab(self):
        f = self._scrollable_tab("Output")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Output File"); r += 1
        self._field(f, r, "output",
                    lambda p: self._entry(p, "output", 36),
                    tip_key="output"); r += 1
        self._field(f, r, "file_override",
                    lambda p: self._combo(p, "file_override", YES_NO),
                    tip_key="file_override"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "CSV Layout"); r += 1
        self._field(f, r, "csv_layout",
                    lambda p: self._combo(p, "csv_layout", ["rows", "columns"]),
                    tip_key="csv_layout"); r += 1
        self._field(f, r, "result_padding",
                    lambda p: self._combo(p, "result_padding", YES_NO),
                    tip_key="result_padding"); r += 1
        self._field(f, r, "split_output",
                    lambda p: self._combo(p, "split_output", ["no", "files", "merged"]),
                    tip_key="split_output"); r += 1

    def _build_reformat_tab(self):
        f = self._scrollable_tab("Reformat")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Reformat"); r += 1
        self._field(f, r, "reformat",
                    lambda p: self._combo(p, "reformat", YES_NO),
                    tip_key="reformat"); r += 1
        self._field(f, r, "label_elements",
                    lambda p: self._entry(p, "label_elements", 20),
                    tip_key="label_elements"); r += 1
        self._field(f, r, "value_elements",
                    lambda p: self._entry(p, "value_elements", 20),
                    tip_key="value_elements"); r += 1
        self._field(f, r, "sort",
                    lambda p: self._combo(p, "sort",
                    ["alphabet", "reverse", "unsorted"]),
                    tip_key="sort"); r += 1
        self._field(f, r, "zero_fill",
                    lambda p: self._combo(p, "zero_fill",
                    ["no", "adjacent", "snapshot"]),
                    tip_key="zero_fill"); r += 1
        self._field(f, r, "fill_first",
                    lambda p: self._combo(p, "fill_first", YES_NO),
                    tip_key="fill_first"); r += 1
        self._field(f, r, "merged_meta",
                    lambda p: self._combo(p, "merged_meta",
                    ["interleaved", "grouped"]),
                    tip_key="merged_meta"); r += 1

    def _build_advanced_tab(self):
        f = self._scrollable_tab("Advanced")
        f.columnconfigure(1, weight=1)
        r = 0

        self._section(f, r, "Fetch Mode"); r += 1
        self._field(f, r, "headless_browser",
                    lambda p: self._combo(p, "headless_browser", YES_NO),
                    tip_key="headless_browser"); r += 1

        self._sep(f, r); r += 1
        self._section(f, r, "Retry & Performance"); r += 1
        self._field(f, r, "min_gap",
                    lambda p: self._entry(p, "min_gap", 10),
                    tip_key="min_gap"); r += 1
        self._field(f, r, "delay",
                    lambda p: self._entry(p, "delay", 10),
                    tip_key="delay"); r += 1
        self._field(f, r, "retries",
                    lambda p: self._entry(p, "retries", 10),
                    tip_key="retries"); r += 1
        self._field(f, r, "fallback_candidates",
                    lambda p: self._entry(p, "fallback_candidates", 10),
                    tip_key="fallback_candidates"); r += 1
        self._field(f, r, "threads",
                    lambda p: self._entry(p, "threads", 10),
                    tip_key="threads"); r += 1

    # ── Main UI assembly ──────────────────────────────────────────────────────
    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)

        self._build_url_tab()
        self._build_elements_tab()
        self._build_schedule_tab()
        self._build_output_tab()
        self._build_reformat_tab()
        self._build_advanced_tab()

        # Button bar
        btn_bar = ttk.Frame(self.root)
        btn_bar.pack(fill="x", padx=6, pady=(0, 4))

        self._start_btn = ttk.Button(btn_bar, text="▶  Start", command=self._start)
        self._start_btn.pack(side="left", padx=(0, 4))

        self._stop_btn = ttk.Button(btn_bar, text="■  Stop", command=self._stop,
                                    state="disabled")
        self._stop_btn.pack(side="left")

        # Right-side settings buttons (packed right-to-left so order reads left-to-right)
        ttk.Button(btn_bar, text="Reset to Defaults",
                   command=self._reset_defaults).pack(side="right")
        ttk.Button(btn_bar, text="Reset to Last Saved",
                   command=self._reset_saved).pack(side="right", padx=(0, 4))
        ttk.Button(btn_bar, text="Save Settings",
                   command=self._save).pack(side="right", padx=(0, 4))

        # Output log
        log_frame = ttk.LabelFrame(self.root, text="Output log")
        log_frame.pack(fill="both", expand=False, padx=6, pady=(0, 6))

        self._log_widget = scrolledtext.ScrolledText(
            log_frame, height=17, wrap="word",
            font=("Consolas", 9), state="disabled")
        self._log_widget.pack(fill="both", expand=True, padx=4, pady=4)

    # ── Element text-widget sync helpers ─────────────────────────────────────
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

    # ── Settings I/O ──────────────────────────────────────────────────────────
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

    def _save(self):
        self._sync_vars_from_text_widgets()
        # For disabled fields the StringVar holds the visual default, not the
        # real value — use _disabled_real_values for those keys.
        raw = {k: self._disabled_real_values.get(k, sv.get())
               for k, sv in self._vars.items()}
        _write_settings(raw)
        self._unsaved = False

    def _reset_saved(self):
        if not tk.messagebox.askyesno(
                "Reset to Last Saved",
                "Discard all unsaved changes and reload from settings.txt?"):
            return
        self._load_from_file()

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

    def _on_close(self):
        if self._unsaved:
            # askyesnocancel: Yes=Save & exit, No=Exit without saving, Cancel=go back
            result = tk.messagebox.askyesnocancel(
                "Unsaved Changes",
                "You have unsaved changes.\nSave before closing?")
            if result is None:   # Cancel — go back to the window
                return
            if result:           # Yes — save then exit
                self._save()
        self.root.destroy()

    # ── Run / Stop ────────────────────────────────────────────────────────────
    def _start(self):
        if self._running:
            return
        self._save()
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.configure(state="disabled")
        self._running = True
        self._proc = None
        self._start_btn.configure(state="disabled", text="Running\u2026")
        self._stop_btn.configure(state="normal")

        flags = 0
        exe = sys.executable
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
            # pythonw.exe suppresses stdout even with a pipe; use python.exe instead
            if exe.lower().endswith("pythonw.exe"):
                exe = os.path.join(os.path.dirname(exe), "python.exe")
        try:
            self._proc = subprocess.Popen(
                [exe, "-u", sys.argv[0], "--worker"],
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

    # ── Log polling ───────────────────────────────────────────────────────────
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
    if "--worker" in sys.argv:
        main()
    else:
        WaybackGUI().run()

import csv
import re
import sys
import time
import os
import calendar
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

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
                               mode    : 'exact' | 'contains' | 'all'
                               negate  : bool  (True when prefixed with '!')

    Token syntax:
      (blank)      – exact URL only, no variants fetched, no post-filtering
      *            – all variants, no post-filtering (include-all)
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
            filters.append({"pattern": t, "mode": "path", "negate": negate})
        elif t.endswith("*"):
            cdx_wildcard = True
            filters.append({"pattern": t[:-1], "mode": "contains", "negate": negate})
        elif t:
            cdx_wildcard = True
            filters.append({"pattern": t, "mode": "exact", "negate": negate})

    return cdx_wildcard, filters


def _single_filter_matches(url: str, pattern: str | None, mode: str,
                           case_sensitive: bool = True) -> bool:
    """Return True if url matches one filter rule (ignoring negate)."""
    if mode == "all":
        return True
    if not case_sensitive and pattern is not None:
        url = url.lower()
        pattern = pattern.lower()
    if mode == "path":
        from urllib.parse import urlparse
        return pattern in urlparse(url).path
    if mode == "contains":
        return pattern in url
    # mode == "exact": pattern must be one of the individual query-string parameters.
    # The Wayback CDX stores URLs with & encoded in several ways:
    #   &amp%3B  (HTML entity &amp; with ; percent-encoded)
    #   \u0026   (JSON-style unicode escape, stored literally)
    # Normalise all variants to plain & before parsing.
    from urllib.parse import urlparse, parse_qs, unquote
    import html
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
                        case_sensitive: bool = True) -> bool:
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

    # ── filter_any (OR) ───────────────────────────────────────────────────────
    any_includes = [f for f in filters_any if not f["negate"]]
    any_excludes = [f for f in filters_any if f["negate"]]

    for f in any_excludes:
        if _single_filter_matches(url, f["pattern"], f["mode"], cs):
            return False

    if any_includes:
        if not any(_single_filter_matches(url, f["pattern"], f["mode"], cs)
                   for f in any_includes):
            return False

    # ── filter_all (AND) ──────────────────────────────────────────────────────
    all_includes = [f for f in filters_all if not f["negate"]]
    all_excludes = [f for f in filters_all if f["negate"]]

    if all_excludes:
        if all(_single_filter_matches(url, f["pattern"], f["mode"], cs)
               for f in all_excludes):
            return False

    if all_includes:
        if not all(_single_filter_matches(url, f["pattern"], f["mode"], cs)
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
    before any subsequent log messages (e.g. end pass notices).
    """
    with _print_lock:
        while _next_to_print[0] in _print_buffer:
            m = _print_buffer.pop(_next_to_print[0])
            print(m)
            _log_lines.append(m)
            _next_to_print[0] += 1


def save_log(output_path: str):
    log_path = os.path.splitext(output_path)[0] + ".log"
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(_log_lines))
        print(f"[Log]    Saved -> {os.path.abspath(log_path)}")
    except Exception as e:
        print(f"[Warning] Could not save log: {e}")


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
        element_1 = <img class="card-img" alt="Cannon Cart" src="...">

    Parent > child chain (space between closing and opening tag):
        element_1 = <div class="card-row"> <span class="value">5%</span>
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
    Used by padding to enumerate gaps.
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


def load_settings(path="settings.txt") -> dict:
    if not os.path.exists(path):
        sys.exit(f"[Error] settings.txt not found at: {os.path.abspath(path)}")

    raw = {
        "url": "",
        "from_date": "",
        "to_date": "",
        "frequency": "monthly",
        "sample_from": "start",
        "convention": "us",
        "date_style": "long",
        "year_digits": "4",
        "date_padding": "no",
        "time_format": "12h",
        "time_padding": "yes",
        "show_seconds": "no",
        "output": "wayback_results.csv",
        "file_override": "yes",
        "show_month": "yes",
        "show_day": "yes",
        "show_year": "yes",
        "show_time": "yes",
        "csv_layout": "rows",
        "padding": "no",
        "filter_any": "",
        "filter_all": "",
        "case_sensitive": "yes",
        "min_gap": "0.5",
        "delay": "10",
        "retries": "5",
        "end_passes": "2",
        "threads": "3",
        "reformat": "no",
        "label_elements": "",
        "value_elements": "",
        "sort": "alphabet",
        "zero_fill": "no",
        "fill_first": "no",
        **{f"element_{i}": "" for i in range(1, MAX_ELEMENTS + 1)},
        **{f"extract_{i}": "text" for i in range(1, MAX_ELEMENTS + 1)},
    }
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower().replace("-", "_")
            # Accept legacy 'url_variants' as an alias for 'filter_any'
            if key == "url_variants":
                key = "filter_any"
                if value.lower() in ("yes", "true", "1"):
                    value = "*"
                elif value.lower() in ("no", "false", "0"):
                    value = ""
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
        # else: filter_any already has tokens; just ensure CDX wildcard fires
        raw["_url_wildcard"] = "yes"
    else:
        raw["_url_wildcard"] = "no"
    if raw["frequency"] not in FREQ_MAP:
        sys.exit(f"[Error] 'frequency' must be one of: {', '.join(FREQ_MAP)}")
    if raw["csv_layout"].lower() not in ("columns", "rows"):
        sys.exit("[Error] 'csv_layout' must be 'columns' or 'rows'")

    try:
        min_gap_frac = float(raw["min_gap"])
        if min_gap_frac < 0:
            raise ValueError
    except ValueError:
        sys.exit("[Error] 'min_gap' must be a number >= 0, e.g. 0.5")

    min_gap_secs = int(FREQ_SECONDS.get(raw["frequency"], 0) * min_gap_frac)

    cdx_any, filters_any = parse_filters(raw["filter_any"])
    cdx_all, filters_all = parse_filters(raw["filter_all"])
    cdx_wildcard = cdx_any or cdx_all or (raw["_url_wildcard"] == "yes")

    if not any([yesno(raw["show_month"]), yesno(raw["show_day"]),
                yesno(raw["show_year"]), yesno(raw["show_time"])]):
        sys.exit("[Error] At least one of show_month/show_day/show_year/show_time must be yes.")

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
        "padding": yesno(raw["padding"]),
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
        "end_passes": int(raw["end_passes"]),
        "threads": threads,
        "reformat": do_reformat,
        "reformat_pairs": reformat_pairs,
        "sort": sort,
        "zero_fill": raw["zero_fill"].strip().lower(),
        "fill_first": yesno(raw["fill_first"]),
    }

    if cfg["zero_fill"] not in ("no", "adjacent", "snapshot"):
        sys.exit("[Error] 'zero_fill' must be 'no', 'adjacent', or 'snapshot'.")
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
                        cfg["case_sensitive"],
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
            if attempt < cfg["retries"]:
                log(f"[CDX]    Query failed: {e} -- retrying in {cfg['delay']}s ...")
                time.sleep(cfg["delay"])
            else:
                sys.exit(f"[CDX]    Query failed after {cfg['retries']} attempts: {e}")


# ── Step 2: Sampling ──────────────────────────────────────────────────────────
def sample_snapshots(snapshots: list, cfg: dict) -> list:
    frequency = cfg["frequency"]
    anchor = cfg["sample_from"]
    min_gap_secs = cfg["min_gap_secs"]
    freq_fmt = FREQ_MAP.get(frequency)
    base_url = cfg["url"]

    def snap_sort_key(s, target):
        """Sort key: canonical URL first (0), then time distance to target."""
        is_variant = 0 if s["original"] == base_url else 1
        time_dist = abs((ts_to_dt(s["timestamp"]) - target).total_seconds())
        return (is_variant, time_dist)

    if freq_fmt is None:
        if min_gap_secs == 0:
            return snapshots
        kept = [snapshots[0]]
        for snap in snapshots[1:]:
            if abs((ts_to_dt(snap["timestamp"]) - 
                    ts_to_dt(kept[-1]["timestamp"])).total_seconds()) >= min_gap_secs:
                kept.append(snap)
        return kept

    buckets: dict = {}
    for snap in snapshots:
        bucket = ts_to_dt(snap["timestamp"]).strftime(freq_fmt)
        buckets.setdefault(bucket, []).append(snap)

    sampled = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        ref_dt = ts_to_dt(group[0]["timestamp"])
        target = anchor_dt_for(ref_dt, frequency, anchor)
        best = min(group, key=lambda s: snap_sort_key(s, target))
        sampled.append(best)

    log(f"[Sample] '{frequency}' ({anchor}) -> {len(sampled)} snapshots selected.")

    if min_gap_secs > 0 and len(sampled) > 1:
        kept = [sampled[0]]
        discarded_dates = []
        for snap in sampled[1:]:
            prev_dt = ts_to_dt(kept[-1]["timestamp"])
            curr_dt = ts_to_dt(snap["timestamp"])
            gap = abs((curr_dt - prev_dt).total_seconds())
            if gap >= min_gap_secs:
                kept.append(snap)
            else:
                prev_anchor = anchor_dt_for(prev_dt, frequency, anchor)
                curr_anchor = anchor_dt_for(curr_dt, frequency, anchor)
                prev_is_variant = kept[-1]["original"] != base_url
                curr_is_variant = snap["original"] != base_url
                prev_dist = abs((prev_dt - prev_anchor).total_seconds())
                curr_dist = abs((curr_dt - curr_anchor).total_seconds())
                # Prefer canonical URL first; use time distance as tiebreaker
                curr_better = (prev_is_variant, prev_dist) > (curr_is_variant, curr_dist)
                if curr_better:
                    discarded_dates.append(kept[-1]["timestamp"])
                    kept[-1] = snap
                else:
                    discarded_dates.append(snap["timestamp"])
        if discarded_dates:
            # Format min_gap_secs as human-readable
            gap_mins = min_gap_secs // 60
            gap_hours = gap_mins // 60
            gap_days = gap_hours // 24
            if gap_days >= 1:
                gap_str = f"{gap_days}d {gap_hours % 24}h" if gap_hours % 24 else f"{gap_days}d"
            elif gap_hours >= 1:
                gap_str = f"{gap_hours}h {gap_mins % 60}m" if gap_mins % 60 else f"{gap_hours}h"
            else:
                gap_str = f"{gap_mins}m"
            dates_str = ", ".join(
                ts_to_dt(ts).strftime("%Y-%m-%d") for ts in discarded_dates
            )
            log(f"[Gap]    {len(discarded_dates)} snapshot(s) discarded (min gap: {gap_str}): {dates_str}")
        sampled = kept

    return sampled


# ── Step 3: Fetch One Snapshot ────────────────────────────────────────────────
def fetch_snapshot(session, index: int, total: int, timestamp: str,
                   original_url: str, cfg: dict) -> dict:
    wayback_url = f"{WAYBACK_BASE}/{timestamp}/{original_url}"
    date_str, time_str = format_datetime(ts_to_dt(timestamp), cfg)
    prefix = f"[{index}/{total}] {date_str} {time_str}".strip()

    max_sel_len = max(len(e["selector"]) for e in cfg["elements"])
    last_err = ""

    for attempt in range(1, cfg["retries"] + 1):
        try:
            resp = session.get(
                wayback_url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")
            elem_values = {}
            lines = [prefix]

            for elem in cfg["elements"]:
                sel_chain = elem["selector_chain"]
                extract = elem["extract"]
                sel_display = elem["selector"]  # last/only step label
                label = f"  {sel_display:<{max_sel_len}}"

                # Walk the selector chain: start from the whole document,
                # then narrow into matching children at each step.
                # Each step is {"sel": str|None, "nth": int|None}.
                #   sel=None  -> pick the nth direct child element of each scope
                #   nth=None  -> keep all matches of sel
                #   nth=N     -> keep only the Nth match of sel (1-based)
                current_scope = [soup]
                for step in sel_chain:
                    sel = step["sel"]
                    nth = step["nth"]
                    next_scope = []
                    for scope in current_scope:
                        if sel is None:
                            # Bare nth-child step: pick among direct element children
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
                    elem_values[elem["slot"]] = values
                    if values:
                        lines.append(f"{label}: {', '.join(values)}")
                    else:
                        lines.append(f"{label}: (no value)")

            buffer_and_flush(index, "\n".join(lines))
            return {
                "timestamp": timestamp,
                "date": date_str,
                "time": time_str,
                "elem_values": elem_values,
                "url": wayback_url,
                "error": "",
            }

        except requests.exceptions.Timeout:
            last_err = "timeout"
        except requests.exceptions.HTTPError as e:
            last_err = f"HTTP {e.response.status_code}"
            if e.response.status_code in (404, 403):
                break
        except Exception as e:
            last_err = str(e)

        if attempt < cfg["retries"]:
            # Append retry notice to this snapshot's pending buffer entry
            with _print_lock:
                pending = _print_buffer.get(index, prefix)
                _print_buffer[index] = pending + f"\n  -> attempt {attempt}/{cfg['retries']} failed: {last_err} -- retrying ..."
            time.sleep(cfg["delay"])

    buffer_and_flush(index, f"{prefix} ... failed ({last_err})")
    return {
        "timestamp": timestamp,
        "date": date_str,
        "time": time_str,
        "elem_values": {elem["slot"]: [] for elem in cfg["elements"]},
        "url": wayback_url,
        "error": last_err,
    }


# ── Result Padding ────────────────────────────────────────────────────────────
def apply_padding(results: list, cfg: dict) -> list:
    """
    When padding is enabled and a regular frequency is in use, return a
    new list that inserts blank entries for every period bucket that had no valid
    snapshot, so the output spans every period continuously between the first and
    last result. Returns the original list unchanged if padding is not applicable.
    """
    frequency = cfg["frequency"]
    if not cfg["padding"] or frequency == "all":
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


# ── Step 4: Write CSV ─────────────────────────────────────────────────────────
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
    # Console shows them comma-separated; CSV expands into separate columns/rows.
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

    Output structure for rows layout:
        col 0         : row label  (date / time / url / error / <label values>)
        cols 1..N     : one per snapshot

    Output structure for columns layout:
        row 0         : col header (date / time / url / error / <label values>)
        rows 1..N     : one per snapshot

    The label element and value element are identified by their slot number.
    For each snapshot the label[i] -> value[i] pairing is built by aligning
    the parallel lists produced by those two elements (rank i of labels pairs
    with rank i of values).

    Special rows/cols carried through:
        date, time   - always first (time only if show_time is enabled)
        url, error   - always placed after date/time, before the label rows/cols
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
    # For each label, compute which column index gets "0":
    #   None  -> no zero for this label
    #   >= 0  -> replace that column's value with "0"
    #   -1    -> needs a new column prepended (fill_first case)
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
                        # Put "0" in the last real (non-padded) snapshot before first
                        real_before = next(
                            (i for i in range(first - 1, -1, -1)
                             if results[i] and results[i].get("timestamp")),
                            None
                        )
                        zero_cols[label] = real_before if real_before is not None else first - 1
                    else:
                        # adjacent: put "0" in the period cell immediately before first
                        zero_cols[label] = first - 1

        # If any label needs column -1, prepend a synthetic column for the
        # period immediately before the dataset starts.
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
            # Shift snap_maps: prepend empty entry so column indices stay aligned
            pair_data = [(ol, [{}] + sm) for ol, sm in pair_data]
            # -1 -> 0; all other non-None values shift up by 1
            zero_cols = {
                lbl: (0 if v == -1 else v + 1 if v is not None else None)
                for lbl, v in zero_cols.items()
            }

    n = len(snap_dates)

    # ── Write output ──────────────────────────────────────────────────────────
    ref_path = os.path.splitext(output_path)[0] + "_reformatted.csv"

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
             total: int, cfg: dict) -> list:
    failed = []
    with requests.Session() as session:
        if cfg["threads"] > 1:
            futures = {}
            with ThreadPoolExecutor(max_workers=cfg["threads"]) as executor:
                for i in indices:
                    snap = snapshots[i]
                    fut = executor.submit(
                        fetch_snapshot, session,
                        i + 1, total,
                        snap["timestamp"], snap["original"], cfg,
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
                    snap["timestamp"], snap["original"], cfg,
                )
                results[i] = result
                if result["error"]:
                    failed.append(i)
    return failed


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start_time = time.time()
    cfg = load_settings("settings.txt")

    sample_dt = datetime(2023, 11, 5, 14, 30, 22)
    sample_date, sample_time = format_datetime(sample_dt, cfg)
    sample_str = " | ".join(p for p in [sample_date, sample_time] if p)
    gap_info = (f"{cfg['min_gap_frac']} × period"
                             if cfg["min_gap_secs"] > 0 else "disabled")

    log("=" * 60)
    log("  Wayback Element Tracker v1.4.3")
    log("=" * 60)
    filter_any_raw = cfg["filter_any_raw"]
    filter_all_raw = cfg["filter_all_raw"]
    filters_any    = cfg["filters_any"]
    filters_all    = cfg["filters_all"]

    def _filter_display(filters, raw):
        if not raw:
            return ""
        if not filters:
            return "(*)"
        parts = []
        for f in filters:
            prefix = "!" if f["negate"] else ""
            if f["mode"] == "all":
                label = f"{prefix}*"
            elif f["mode"] == "path":
                label = f"{prefix}{f['pattern']}"
            elif f["mode"] == "contains":
                label = f"{prefix}{f['pattern']}*"
            else:
                label = f"{prefix}{f['pattern']}"
            parts.append(label)
        return ", ".join(parts)

    case_tag = "case-sensitive" if cfg["case_sensitive"] else "case-insensitive"
    any_display = _filter_display(filters_any, filter_any_raw)
    all_display = _filter_display(filters_all, filter_all_raw)

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
    log(f"  CSV layout : {cfg['csv_layout']}  |  result padding: {'yes' if cfg['padding'] else 'no'}")
    override_str = "yes" if cfg["file_override"] else "no"
    log(f"  Output     : {cfg['output']}  |  override: {override_str}")
    if cfg["reformat"]:
        pairs_str = "  ".join(f"{ls}->{vs}" for ls, vs in cfg["reformat_pairs"])
        log(f"  Reformat   : yes  |  pairs: {pairs_str}  |  sort: {cfg['sort']}")
    log("=" * 60)

    snapshots = get_snapshots(cfg)
    if not snapshots:
        sys.exit("No snapshots to process.")

    snapshots = sample_snapshots(snapshots, cfg)
    total = len(snapshots)
    results = [None] * total

    failed_indices = run_pass(list(range(total)), snapshots, results, total, cfg)
    drain_buffer()

    for pass_num in range(1, cfg["end_passes"] + 1):
        if not failed_indices:
            break
        log(f"\n[End pass {pass_num}/{cfg['end_passes']}] Retrying {len(failed_indices)} failed snapshot(s) ...")
        time.sleep(cfg["delay"])
        failed_indices = run_pass(failed_indices, snapshots, results, total, cfg)
        drain_buffer()

    output_path = resolve_output_path(cfg["output"], cfg["file_override"])
    if output_path != cfg["output"]:
        log(f"[CSV]    '{cfg['output']}' already exists -- writing to '{output_path}' instead.")
    write_csv(results, cfg, output_path)
    if cfg["reformat"]:
        reformat_csv(results, cfg, output_path)
    save_log(output_path)

    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    log(f"[Done]   Finished in {mins}m {secs}s")


if __name__ == "__main__":
    main()

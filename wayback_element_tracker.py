import csv
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
CDX_API      = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"

FREQ_MAP = {
    "all":     None,
    "hourly":  "%Y%m%d%H",
    "daily":   "%Y%m%d",
    "weekly":  "%Y%W",
    "monthly": "%Y%m",
    "yearly":  "%Y",
}

FREQ_SECONDS = {
    "all":     0,
    "hourly":  3600,
    "daily":   86400,
    "weekly":  604800,
    "monthly": 2592000,
    "yearly":  31536000,
}

KNOWN_ATTRS = [
    "title", "href", "src", "value", "content",
    "alt", "placeholder", "datetime", "action",
]

MAX_ELEMENTS = 5

# ── Logging & Sequential Print Buffer ────────────────────────────────────────
_log_lines     = []
_print_lock    = threading.Lock()
_print_buffer  = {}     # index -> message string
_next_to_print = [1]    # list so threads share the same mutable object

def log(msg: str = ""):
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
    Parse a raw HTML snippet into (css_selector, extractable_attrs).

    Selector strategy: always tag.class1.class2#id so the selector is
    as specific as possible and won't match unrelated elements that happen
    to share a class or id alone.
    """
    soup = BeautifulSoup(raw.strip(), "lxml")
    element = None
    for tag in soup.find_all(True):
        if tag.name not in ("html", "body"):
            element = tag
            break
    if element is None:
        sys.exit(
            f"[Error] Could not parse element_{slot} in settings.txt.\n"
            f"        Paste the full HTML tag, e.g.:\n"
            f"        element_{slot} = <p class=\"rbx-lead\" title=\"28,760,666\">28M+</p>"
        )

    elem_id      = element.get("id", "").strip()
    classes      = element.get("class", [])
    extractables = get_extractable_attrs(element)

    # Selector strategy: always build tag.class1.class2#id using whatever
    # the pasted element provides.  Classes are included even when an id is
    # present so that, if the same id appears on multiple elements, the
    # class list narrows the match to the intended one.
    # Trade-off: if the live page adds extra classes not present in the
    # pasted snippet, the selector won't match — remove the class portion
    # manually if that happens.
    selector = element.name
    if classes:
        selector += "." + ".".join(classes)
    if elem_id:
        selector += "#" + elem_id

    return selector, extractables


# ── Date / Time Formatting ────────────────────────────────────────────────────
def format_date(dt: datetime, cfg: dict) -> str:
    show_month = cfg["show_month"]
    show_day   = cfg["show_day"]
    show_year  = cfg["show_year"]
    convention = cfg["convention"]
    style      = cfg["date_style"]
    pad        = cfg["date_padding"]
    year_dig   = cfg["year_digits"]

    day_str    = f"{dt.day:02d}" if pad else str(dt.day)
    month_str  = f"{dt.month:02d}" if pad else str(dt.month)
    month_long = dt.strftime("%B")
    month_abbr = dt.strftime("%b")
    year       = dt.strftime("%Y") if year_dig == 4 else dt.strftime("%y")

    if style in ("long", "short"):
        month_word = month_long if style == "long" else month_abbr
        if convention == "us":
            parts = []
            if show_month: parts.append(month_word)
            if show_day:   parts.append(str(dt.day) + ",")
            if show_year:  parts.append(year)
            return " ".join(parts).rstrip(",").strip()
        else:
            parts = []
            if show_day:   parts.append(str(dt.day))
            if show_month: parts.append(month_word)
            if show_year:  parts.append(year)
            return " ".join(parts).strip()
    else:
        if convention == "us":
            components = []
            if show_month: components.append(month_str)
            if show_day:   components.append(day_str)
            if show_year:  components.append(year)
        else:
            components = []
            if show_day:   components.append(day_str)
            if show_month: components.append(month_str)
            if show_year:  components.append(year)
        return "/".join(components)


def format_time(dt: datetime, cfg: dict) -> str:
    fmt      = cfg["time_format"]
    seconds  = cfg["show_seconds"]
    time_pad = cfg["time_padding"]
    minute   = dt.strftime("%M")
    second   = dt.strftime("%S")
    if fmt == "12h":
        hour   = dt.strftime("%I").lstrip("0") or "12"
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


# ── Settings Parser ───────────────────────────────────────────────────────────
def yesno(val: str) -> bool:
    return val.strip().lower() == "yes"

def load_settings(path="settings.txt") -> dict:
    if not os.path.exists(path):
        sys.exit(f"[Error] settings.txt not found at: {os.path.abspath(path)}")

    raw = {
        "url":           "",
        "from_date":     "",
        "to_date":       "",
        "frequency":     "monthly",
        "sample_anchor": "start",
        "convention":    "us",
        "date_style":    "long",
        "year_digits":   "4",
        "date_padding":  "no",
        "time_format":   "12h",
        "time_padding":  "yes",
        "show_seconds":  "no",
        "output":        "wayback_results.csv",
        "show_month":    "yes",
        "show_day":      "yes",
        "show_year":     "yes",
        "show_time":     "yes",
        "csv_layout":    "rows",
        "min_gap":       "0.5",
        "delay":         "10",
        "retries":       "5",
        "end_passes":    "2",
        "threads":       "3",
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
            key   = key.strip().lower().replace("-", "_")
            value = value.strip()
            if key in raw and value:
                raw[key] = value

    if not raw["url"]:
        sys.exit("[Error] 'url' is missing from settings.txt")
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

    if not any([yesno(raw["show_month"]), yesno(raw["show_day"]),
                yesno(raw["show_year"]), yesno(raw["show_time"])]):
        sys.exit("[Error] At least one of show_month/show_day/show_year/show_time must be yes.")

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
        selector, extractables = parse_element_html(html, i)
        if extract != "text" and extract not in extractables:
            others = [a for a in extractables if a != extract]
            msg = (
                f"[Warning] extract_{i} = '{extract}' not found on pasted element '{selector}'.\n"
                f"          It may still exist in live snapshots."
            )
            if others:
                msg += f"\n          Other available: {', '.join(others)}"
            print(msg)
        elements.append({"slot": i, "selector": selector, "extract": extract})

    if not elements:
        sys.exit("[Error] At least one element_1 through element_5 must be set.")

    try:
        threads = int(raw["threads"])
        if threads < 1:
            raise ValueError
    except ValueError:
        sys.exit("[Error] 'threads' must be a positive integer.")

    return {
        "url":           raw["url"],
        "elements":      elements,
        "from_date":     raw["from_date"],
        "to_date":       raw["to_date"],
        "frequency":     raw["frequency"],
        "sample_anchor": raw["sample_anchor"].lower(),
        "convention":    raw["convention"].lower(),
        "date_style":    raw["date_style"].lower(),
        "year_digits":   int(raw["year_digits"]),
        "date_padding":  yesno(raw["date_padding"]),
        "time_format":   raw["time_format"].lower(),
        "time_padding":  yesno(raw["time_padding"]),
        "show_seconds":  yesno(raw["show_seconds"]),
        "show_month":    yesno(raw["show_month"]),
        "show_day":      yesno(raw["show_day"]),
        "show_year":     yesno(raw["show_year"]),
        "show_time":     yesno(raw["show_time"]),
        "csv_layout":    raw["csv_layout"].lower(),
        "min_gap_secs":  min_gap_secs,
        "min_gap_frac":  float(raw["min_gap"]),
        "output":        raw["output"],
        "delay":         float(raw["delay"]),
        "retries":       int(raw["retries"]),
        "end_passes":    int(raw["end_passes"]),
        "threads":       threads,
    }


# ── Step 1: Get Snapshot List ─────────────────────────────────────────────────
def get_snapshots(cfg: dict) -> list:
    params = {
        "url":      cfg["url"],
        "output":   "json",
        "fl":       "timestamp,original",
        "collapse": "digest",
        "filter":   "statuscode:200",
    }
    if cfg["from_date"]:
        params["from"] = cfg["from_date"]
    if cfg["to_date"]:
        params["to"] = cfg["to_date"]

    log(f"[CDX]    Querying snapshots for: {cfg['url']}")
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
            return snapshots
        except Exception as e:
            if attempt < cfg["retries"]:
                log(f"[CDX]    Query failed: {e} -- retrying in {cfg['delay']}s ...")
                time.sleep(cfg["delay"])
            else:
                sys.exit(f"[CDX]    Query failed after {cfg['retries']} attempts: {e}")


# ── Step 2: Sampling ──────────────────────────────────────────────────────────
def sample_snapshots(snapshots: list, cfg: dict) -> list:
    frequency    = cfg["frequency"]
    anchor       = cfg["sample_anchor"]
    min_gap_secs = cfg["min_gap_secs"]
    freq_fmt     = FREQ_MAP.get(frequency)

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
        group  = buckets[bucket]
        ref_dt = ts_to_dt(group[0]["timestamp"])
        target = anchor_dt_for(ref_dt, frequency, anchor)
        best   = min(group, key=lambda s: abs(
            (ts_to_dt(s["timestamp"]) - target).total_seconds()
        ))
        sampled.append(best)

    log(f"[Sample] '{frequency}' ({anchor}) -> {len(sampled)} snapshots selected.")

    if min_gap_secs > 0 and len(sampled) > 1:
        kept = [sampled[0]]
        discarded_dates = []
        for snap in sampled[1:]:
            prev_dt = ts_to_dt(kept[-1]["timestamp"])
            curr_dt = ts_to_dt(snap["timestamp"])
            gap     = abs((curr_dt - prev_dt).total_seconds())
            if gap >= min_gap_secs:
                kept.append(snap)
            else:
                prev_anchor = anchor_dt_for(prev_dt, frequency, anchor)
                curr_anchor = anchor_dt_for(curr_dt, frequency, anchor)
                if abs((curr_dt - curr_anchor).total_seconds()) < \
                   abs((prev_dt - prev_anchor).total_seconds()):
                    discarded_dates.append(kept[-1]["timestamp"])
                    kept[-1] = snap
                else:
                    discarded_dates.append(snap["timestamp"])
        if discarded_dates:
            # Format min_gap_secs as human-readable
            gap_mins  = min_gap_secs // 60
            gap_hours = gap_mins // 60
            gap_days  = gap_hours // 24
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
    last_err    = ""

    for attempt in range(1, cfg["retries"] + 1):
        try:
            resp = session.get(
                wayback_url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()

            soup        = BeautifulSoup(resp.text, "lxml")
            elem_values = {}
            lines       = [prefix]

            for elem in cfg["elements"]:
                sel     = elem["selector"]
                extract = elem["extract"]
                matches = soup.select(sel)
                label   = f"  {sel:<{max_sel_len}}"
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
                "date":        date_str,
                "time":        time_str,
                "elem_values": elem_values,
                "url":         wayback_url,
                "error":       "",
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
        "date":        date_str,
        "time":        time_str,
        "elem_values": {elem["slot"]: [] for elem in cfg["elements"]},
        "url":         wayback_url,
        "error":       last_err,
    }


# ── Step 4: Write CSV ─────────────────────────────────────────────────────────
def write_csv(results: list, cfg: dict, output_path: str) -> None:
    if not results:
        log("[CSV]    No results to write.")
        return

    show_time = cfg["show_time"]
    elements  = cfg["elements"]
    layout    = cfg["csv_layout"]

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
        slot  = elem["slot"]
        sel   = elem["selector"]
        count = max_per_slot[slot]
        if count == 1:
            def make_fn(s):
                return lambda r: r["elem_values"].get(s, [""])[:1] or [""]
            descriptors.append((sel, make_fn(slot)))
        else:
            for i in range(count):
                label = f"{sel} [{i+1}]"
                def make_fn(s, idx):
                    return lambda r: [(r["elem_values"].get(s, []) + [""] * (idx + 1))[idx]]
                descriptors.append((label, make_fn(slot, i)))

    descriptors.append(("url",   lambda r, _=None: [r["url"]]))
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

    log(f"\n[CSV]    Saved {len(results)} snapshots -> {os.path.abspath(output_path)}")


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
                    fut  = executor.submit(
                        fetch_snapshot, session,
                        i + 1, total,
                        snap["timestamp"], snap["original"], cfg,
                    )
                    futures[fut] = i
                for fut in as_completed(futures):
                    i          = futures[fut]
                    result     = fut.result()
                    results[i] = result
                    if result["error"]:
                        failed.append(i)
            failed.sort()
        else:
            for i in indices:
                snap       = snapshots[i]
                result     = fetch_snapshot(
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
    cfg        = load_settings("settings.txt")

    sample_dt             = datetime(2023, 11, 5, 14, 30, 22)
    sample_date, sample_time = format_datetime(sample_dt, cfg)
    sample_str            = " | ".join(p for p in [sample_date, sample_time] if p)
    gap_info              = (f"{cfg['min_gap_frac']} × period"
                             if cfg["min_gap_secs"] > 0 else "disabled")

    log("=" * 60)
    log("  Wayback Element Tracker v1.0.2")
    log("=" * 60)
    log(f"  URL        : {cfg['url']}")
    for elem in cfg["elements"]:
        log(f"  Element {elem['slot']}  : {elem['selector']}  (extract: {elem['extract']})")
    log(f"  Date range : {cfg['from_date'] or 'start'} -> {cfg['to_date'] or 'now'}")
    log(f"  Frequency  : {cfg['frequency']}  |  anchor: {cfg['sample_anchor']}  |  min gap: {gap_info}")
    log(f"  Format     : {sample_str}")
    log(f"  Threads    : {cfg['threads']}")
    log(f"  CSV layout : {cfg['csv_layout']}")
    log(f"  Output     : {cfg['output']}")
    log("=" * 60)

    snapshots = get_snapshots(cfg)
    if not snapshots:
        sys.exit("No snapshots to process.")

    snapshots = sample_snapshots(snapshots, cfg)
    total     = len(snapshots)
    results   = [None] * total

    failed_indices = run_pass(list(range(total)), snapshots, results, total, cfg)

    for pass_num in range(1, cfg["end_passes"] + 1):
        if not failed_indices:
            break
        log(f"\n[End pass {pass_num}/{cfg['end_passes']}] Retrying {len(failed_indices)} failed snapshot(s) ...")
        time.sleep(cfg["delay"])
        failed_indices = run_pass(failed_indices, snapshots, results, total, cfg)

    write_csv(results, cfg, cfg["output"])
    save_log(cfg["output"])

    elapsed    = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)
    log(f"[Done]   Finished in {mins}m {secs}s")


if __name__ == "__main__":
    main()
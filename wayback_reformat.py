"""
wayback_reformat.py  –  CSV reformatter for Wayback Element Tracker

Contains all functions that pivot / reformat raw CSV output:
    reformat_csv        – pivot a single results list into the reformatted layout
    reformat_merged_csv – pivot a merged (multi-group) results set
    url_slug            – extract a meaningful slug from a URL label
    _merge_key          – normalised key for label de-duplication
    _apply_label_case   – apply case transformation to a label string

These are imported by wayback_element_tracker.py after a run completes.

Standalone usage: reformat an existing raw CSV without re-fetching data.
    python wayback_reformat.py                          # prompts for file(s)
    python wayback_reformat.py results/raw/out.csv      # explicit path(s)
    python wayback_reformat.py raw/*.csv
"""

import csv
import os
import re
import sys
from urllib.parse import urlparse, unquote

# Shared utilities imported from the main tracker.
# This import works without a circular-import error because wayback_element_tracker
# defines these functions before it imports from this module.
from wayback_element_tracker import (
    log,
    apply_padding,
    prev_period_dt,
    ts_to_dt,
    format_datetime,
    _result_key,
    FREQ_MAP,
)
from wayback_combine import resolve_output_path

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
        snap_keys   = [_result_key(r) if r else "" for r in g_results]
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

        return snap_dates, snap_keys, snap_times, snap_urls, snap_errors, pair_data, zero_cols

    group_data = [(suffix, build_group_data(g)) for suffix, g in groups.items()]

    with open(ref_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if layout == "rows":
            # Build unified column order keyed by unique result key, not display
            # date string, for the same reason as write_merged_csv: hidden date
            # fields must not cause snapshots to share a column or lose data.
            all_cols: list = []
            seen_keys: set = set()
            for _, (sdates, skeys, *_) in group_data:
                for k, d in zip(skeys, sdates):
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        all_cols.append((k, d))
            all_cols.sort(key=lambda x: x[0])
            key_order   = [k for k, _ in all_cols]
            label_order = [d for _, d in all_cols]

            writer.writerow(["date"] + label_order)
            if show_time:
                time_lookup: dict = {}
                for _, (sdates, skeys, stimes, *_) in group_data:
                    for k, t in zip(skeys, stimes):
                        if k and k not in time_lookup:
                            time_lookup[k] = t
                writer.writerow(["time"] + [time_lookup.get(k, "") for k in key_order])

            if merged_meta == "interleaved":
                # Per group: label, url, error, then data rows
                for suffix, (sdates, skeys, _, surls, serrs, pdata, zero_cols) in group_data:
                    key_idx = {k: i for i, k in enumerate(skeys)}
                    writer.writerow([suffix])
                    writer.writerow([f"url ({suffix})"] +
                                    [surls[key_idx[k]] if k in key_idx else "" for k in key_order])
                    writer.writerow([f"error ({suffix})"] +
                                    [serrs[key_idx[k]] if k in key_idx else "" for k in key_order])
                    for ordered, snap_maps in pdata:
                        for label in ordered:
                            zc  = zero_cols.get(label) if zero_fill != "no" else None
                            row = []
                            for k in key_order:
                                if k in key_idx:
                                    i = key_idx[k]
                                    row.append("0" if zc is not None and i == zc
                                               else snap_maps[i].get(label, ""))
                                else:
                                    row.append("")
                            writer.writerow([f"{label} ({suffix})"] + row)

            else:  # grouped: all data first, then all urls, then all errors
                for suffix, (sdates, skeys, _, surls, serrs, pdata, zero_cols) in group_data:
                    key_idx = {k: i for i, k in enumerate(skeys)}
                    writer.writerow([suffix])
                    for ordered, snap_maps in pdata:
                        for label in ordered:
                            zc  = zero_cols.get(label) if zero_fill != "no" else None
                            row = []
                            for k in key_order:
                                if k in key_idx:
                                    i = key_idx[k]
                                    row.append("0" if zc is not None and i == zc
                                               else snap_maps[i].get(label, ""))
                                else:
                                    row.append("")
                            writer.writerow([f"{label} ({suffix})"] + row)

                writer.writerow([])  # blank separator before meta rows
                for suffix, (sdates, skeys, _, surls, serrs, *_) in group_data:
                    key_idx = {k: i for i, k in enumerate(skeys)}
                    writer.writerow([f"url ({suffix})"] +
                                    [surls[key_idx[k]] if k in key_idx else "" for k in key_order])
                for suffix, (sdates, skeys, _, surls, serrs, *_) in group_data:
                    key_idx = {k: i for i, k in enumerate(skeys)}
                    writer.writerow([f"error ({suffix})"] +
                                    [serrs[key_idx[k]] if k in key_idx else "" for k in key_order])

        else:  # columns: stack each group's block vertically
            first_block = True
            for suffix, (sdates, _, stimes, surls, serrs, pdata, zero_cols) in group_data:
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


# ---------------------------------------------------------------------------
# CSV reading helpers (standalone mode only)
# ---------------------------------------------------------------------------

_NUMBERED_RE = re.compile(r'^(.+) \[(\d+)\] \(([^)]+)\)$')
_SIMPLE_RE   = re.compile(r'^(.+) \(([^)]+)\)$')


def _header_to_elem(header: str, elements: list):
    """Map a CSV column/row header back to (slot, within_slot_index), or None."""
    m = _NUMBERED_RE.match(header)
    if m:
        sel, n, ext = m.group(1), int(m.group(2)), m.group(3)
        for elem in elements:
            if elem["selector"] == sel and elem["extract"] == ext:
                return elem["slot"], n - 1
        return None
    m2 = _SIMPLE_RE.match(header)
    if m2:
        sel, ext = m2.group(1), m2.group(2)
        for elem in elements:
            if elem["selector"] == sel and elem["extract"] == ext:
                return elem["slot"], 0
    return None


def _ts_from_url(url: str) -> str:
    """Extract a 14-digit Wayback timestamp from a URL, or '' if absent."""
    m = re.search(r'/web/(\d{14})/', url)
    return m.group(1) if m else ""


def _read_columns_layout(path: str, cfg: dict) -> list:
    """Read a columns-layout raw CSV back into the results list format."""
    elements = cfg["elements"]

    with open(path, newline="", encoding="utf-8-sig") as f:
        headers = [h.strip() for h in next(csv.reader(f), [])]

    header_slot_map = {}
    for i, h in enumerate(headers):
        if h.lower() in ("date", "time", "url", "error"):
            continue
        mapped = _header_to_elem(h, elements)
        if mapped:
            header_slot_map[i] = mapped

    has_time = "time" in [h.lower() for h in headers]
    results  = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            elem_values = {elem["slot"]: [] for elem in elements}
            for i, h in enumerate(headers):
                if i not in header_slot_map:
                    continue
                slot, idx = header_slot_map[i]
                vals = elem_values[slot]
                while len(vals) <= idx:
                    vals.append("")
                vals[idx] = row.get(h, "")

            url_val = row.get("url", "")
            results.append({
                "timestamp":   _ts_from_url(url_val),
                "date":        row.get("date", ""),
                "time":        row.get("time", "") if has_time else "",
                "url":         url_val,
                "error":       row.get("error", ""),
                "elem_values": elem_values,
            })
    return results


def _read_rows_layout(path: str, cfg: dict) -> list:
    """Read a rows-layout raw CSV back into the results list format."""
    elements = cfg["elements"]

    with open(path, newline="", encoding="utf-8-sig") as f:
        all_rows = list(csv.reader(f))

    if not all_rows:
        return []

    row_map    = {r[0].strip(): r[1:] for r in all_rows if r}
    date_vals  = row_map.get("date",  [])
    time_vals  = row_map.get("time",  [])
    url_vals   = row_map.get("url",   [])
    error_vals = row_map.get("error", [])
    n          = len(date_vals)

    slot_data: dict = {elem["slot"]: {} for elem in elements}
    for label, vals in row_map.items():
        if label.lower() in ("date", "time", "url", "error"):
            continue
        mapped = _header_to_elem(label, elements)
        if mapped:
            slot, idx = mapped
            slot_data[slot][idx] = vals

    results = []
    for i in range(n):
        elem_values = {}
        for elem in elements:
            slot = elem["slot"]
            d = slot_data.get(slot, {})
            max_idx = max(d.keys(), default=-1)
            vals_list = [(d[idx][i] if i < len(d[idx]) else "") for idx in range(max_idx + 1)]
            elem_values[slot] = vals_list

        url_val = url_vals[i] if i < len(url_vals) else ""
        results.append({
            "timestamp":   _ts_from_url(url_val),
            "date":        date_vals[i]  if i < len(date_vals)  else "",
            "time":        time_vals[i]  if i < len(time_vals)  else "",
            "url":         url_val,
            "error":       error_vals[i] if i < len(error_vals) else "",
            "elem_values": elem_values,
        })
    return results


def _read_csv_to_results(path: str, cfg: dict) -> list:
    """Detect layout and reconstruct a results list from a raw wayback CSV."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        first_row = next(csv.reader(f), [])
    layout = "columns" if "url" in [c.strip().lower() for c in first_row] else "rows"
    return _read_columns_layout(path, cfg) if layout == "columns" else _read_rows_layout(path, cfg)


# ---------------------------------------------------------------------------
# Standalone reformatter entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Import load_settings lazily here to avoid any awkward top-level ordering.
    from wayback_element_tracker import load_settings

    args  = sys.argv[1:]
    cfg   = load_settings()
    cfg["reformat"] = True  # always active when run standalone

    # Split args into file paths and inline pair specs (e.g. "1:2")
    _PAIR_RE = re.compile(r'^(\d+):(\d+)$')
    paths       = [a for a in args if not _PAIR_RE.match(a)]
    inline_pairs = [_PAIR_RE.match(a).groups() for a in args if _PAIR_RE.match(a)]

    if inline_pairs or not cfg.get("reformat_pairs"):
        elements = cfg.get("elements", [])
        if not elements:
            sys.exit("Error: No elements configured in settings.txt.")

        if inline_pairs:
            pairs = []
            for ls, vs in inline_pairs:
                li, vi = int(ls) - 1, int(vs) - 1
                if 0 <= li < len(elements) and 0 <= vi < len(elements):
                    pairs.append((elements[li]["slot"], elements[vi]["slot"]))
                else:
                    sys.exit(f"Error: Pair {ls}:{vs} out of range (1-{len(elements)}).")
            cfg["reformat_pairs"] = pairs
        else:
            print("No label/value element pairs configured in settings.txt.")
            print("Available elements:")
            for i, elem in enumerate(elements, 1):
                print(f"  {i}: {elem['selector']} ({elem['extract']})")
            print()

            pairs = []
            while True:
                label_in = input("  Label element number (blank to finish): ").strip()
                if not label_in:
                    break
                value_in = input("  Value element number: ").strip()
                if not value_in:
                    break
                try:
                    li = int(label_in) - 1
                    vi = int(value_in) - 1
                    if 0 <= li < len(elements) and 0 <= vi < len(elements):
                        pairs.append((elements[li]["slot"], elements[vi]["slot"]))
                    else:
                        print(f"  [Warning] Number out of range (1-{len(elements)}).")
                except ValueError:
                    print("  [Warning] Please enter a number.")

            if not pairs:
                sys.exit("No pairs entered — nothing to reformat.")
            cfg["reformat_pairs"] = pairs

    if not paths:
        print("Enter path(s) to raw CSV file(s) to reformat (blank line to finish):\n")
        while True:
            p = input("  File: ").strip()
            if not p:
                break
            paths.append(p)

    if not paths:
        sys.exit("No files specified.")

    any_ok = False
    for csv_path in paths:
        csv_path = os.path.normpath(csv_path)
        if not os.path.isfile(csv_path):
            print(f"\n[Skip]   File not found: {csv_path}")
            continue

        print(f"\n[Reformat] Reading: {csv_path}")
        results = _read_csv_to_results(csv_path, cfg)

        if not results:
            print(f"[Skip]   No data found in {csv_path}")
            continue

        cfg["ref_dir"] = os.path.dirname(os.path.abspath(csv_path))
        reformat_csv(results, cfg, csv_path)
        any_ok = True

    print("\n[Done]" if any_ok else "\n[Done]   No files were successfully reformatted.")

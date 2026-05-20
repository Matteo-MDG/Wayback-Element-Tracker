"""
wayback_combine.py  –  CSV writer / combiner for Wayback Element Tracker

Contains all functions that write raw CSV output:
    write_csv          – write a single results list to CSV
    write_merged_csv   – write multiple filter groups into one merged CSV
    resolve_output_path – safe output path with optional numeric suffix
    get_output_dirs    – derive base / raw / reformatted folder paths

These are imported by wayback_element_tracker.py after a run completes.

Standalone usage: merge existing raw CSV files into one without re-fetching.
    python wayback_combine.py                          # prompts for files
    python wayback_combine.py a.csv b.csv -o out.csv  # explicit paths
    python wayback_combine.py raw/*.csv --override     # overwrite output
"""

import csv
import os
import re
import sys

# Shared utilities imported from the main tracker.
# This import works without a circular-import error because wayback_element_tracker
# defines these functions before it imports from this module.
from wayback_element_tracker import (
    log,
    apply_padding,
    _result_key,
    ts_to_dt,
    FREQ_MAP,
)

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
        # Build unified sorted column list keyed by unique result key, not by
        # the display date string.  Two snapshots with the same display label
        # (because some date fields are hidden) must still occupy separate
        # columns; the display string is only used for the header row.
        all_cols: list = []
        seen_keys: set = set()
        for _, g_results in padded_groups:
            for r in g_results:
                if not r:
                    continue
                k = _result_key(r)
                if k and k not in seen_keys:
                    seen_keys.add(k)
                    all_cols.append((k, r["date"]))
        all_cols.sort(key=lambda x: x[0])
        key_order   = [k for k, _ in all_cols]   # unique internal keys (timestamps / pad keys)
        label_order = [d for _, d in all_cols]   # display strings for the header row only

        def group_lookup(g_results):
            """Build key->result mapping. Each result has a unique key so
            no data is ever dropped regardless of display date collisions."""
            lookup = {}
            for r in g_results:
                if not r:
                    continue
                k = _result_key(r)
                if k:
                    lookup[k] = r
            return lookup

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            writer.writerow(["date"] + label_order)
            if show_time:
                lookups = [group_lookup(g) for _, g in padded_groups]
                time_row = ["time"]
                for k in key_order:
                    val = next((lk[k]["time"] for lk in lookups if k in lk), "")
                    time_row.append(val)
                writer.writerow(time_row)

            for suffix, g_results in padded_groups:
                lk = group_lookup(g_results)

                # Group label row (suffix in col 0, rest blank)
                writer.writerow([suffix])

                # url and error labelled with suffix
                writer.writerow([f"url ({suffix})"] +
                                 [lk[k]["url"]   if k in lk else "" for k in key_order])
                writer.writerow([f"error ({suffix})"] +
                                 [lk[k]["error"] if k in lk else "" for k in key_order])

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
                        for k in key_order:
                            r    = lk.get(k)
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


# ---------------------------------------------------------------------------
# Standalone CSV combiner entry point
# ---------------------------------------------------------------------------

def _detect_layout(path: str) -> str:
    """Return 'columns' or 'rows' by inspecting the first row of the CSV."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        first_row = next(reader, [])
    return "columns" if "url" in [c.strip().lower() for c in first_row] else "rows"


def _ts_from_url(url: str) -> str:
    """Extract a 14-digit Wayback timestamp from a URL, or '' if absent."""
    m = re.search(r'/web/(\d{14})/', url)
    return m.group(1) if m else ""


def _sort_key_columns(row: dict) -> str:
    ts = _ts_from_url(row.get("url", ""))
    return ts if ts else row.get("date", "")


def _combine_columns(paths: list, output_path: str, override: bool):
    """Merge columns-layout CSV files and write a combined output."""
    reference_headers = None
    all_rows: list = []
    seen_keys: set = set()

    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]

        if reference_headers is None:
            reference_headers = headers
        else:
            if set(headers) != set(reference_headers):
                print(f"[Skip]   Column mismatch in '{path}' — skipping.")
                continue
            if headers != reference_headers:
                rows = [{k: r.get(k, "") for k in reference_headers} for r in rows]

        added = 0
        for row in rows:
            ts  = _ts_from_url(row.get("url", ""))
            key = ts if ts else row.get("date", "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            all_rows.append(row)
            added += 1
        print(f"[Read]   {path}  →  {len(rows)} rows ({added} new)")

    if not all_rows:
        print("[Error]  No rows collected — nothing to write.")
        return

    all_rows.sort(key=_sort_key_columns)
    out = resolve_output_path(output_path, override)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=reference_headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n[Done]   {len(all_rows)} snapshots  →  {os.path.abspath(out)}")


def _combine_rows(paths: list, output_path: str, override: bool):
    """Merge rows-layout CSV files and write a combined output."""
    reference_labels = None
    snap_dict: dict  = {}
    snap_key_order: list = []

    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            all_rows = list(reader)

        if not all_rows:
            continue

        row_map   = {r[0].strip(): r[1:] for r in all_rows if r}
        snap_hdrs = all_rows[0][1:] if all_rows else []
        row_labels = [r[0].strip() for r in all_rows if r]

        if reference_labels is None:
            reference_labels = row_labels
        elif set(row_labels) != set(reference_labels):
            print(f"[Skip]   Row-label mismatch in '{path}' — skipping.")
            continue

        url_row  = row_map.get("url",  [])
        date_row = row_map.get("date", [])
        added = 0

        for i, _ in enumerate(snap_hdrs):
            url_val  = url_row[i]  if i < len(url_row)  else ""
            date_val = date_row[i] if i < len(date_row) else ""
            ts  = _ts_from_url(url_val)
            key = ts if ts else date_val
            if key in snap_dict:
                continue
            snap_dict[key] = {label: (row_map.get(label, [])[i]
                                      if i < len(row_map.get(label, [])) else "")
                              for label in row_labels}
            snap_key_order.append(key)
            added += 1

        print(f"[Read]   {path}  →  {len(snap_hdrs)} snapshots ({added} new)")

    if not snap_dict:
        print("[Error]  No snapshots collected — nothing to write.")
        return

    snap_key_order.sort()
    out = resolve_output_path(output_path, override)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for label in reference_labels:
            writer.writerow([label] + [snap_dict[k].get(label, "") for k in snap_key_order])

    print(f"\n[Done]   {len(snap_key_order)} snapshots  →  {os.path.abspath(out)}")


def _parse_args(argv: list):
    paths, output, override = [], None, False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-o", "--output") and i + 1 < len(argv):
            output = argv[i + 1]; i += 2
        elif arg == "--override":
            override = True; i += 1
        elif not arg.startswith("-"):
            paths.append(arg); i += 1
        else:
            print(f"[Warning] Unknown argument: {arg}"); i += 1
    return paths, output, override


if __name__ == "__main__":
    paths, output, override = _parse_args(sys.argv[1:])
    paths = [p for p in paths if not p.rstrip("/\\").endswith("_reformatted.csv")]

    if not paths:
        print("Enter path(s) to CSV file(s) to combine (blank line to finish):\n")
        while True:
            p = input("  File: ").strip()
            if not p:
                break
            if not p.endswith("_reformatted.csv"):
                paths.append(p)

    if len(paths) < 2:
        sys.exit(
            "At least 2 CSV files are required.\n"
            "Usage: python wayback_combine.py <file1.csv> <file2.csv> [...] [-o output.csv]"
        )

    valid = [p for p in paths if os.path.isfile(os.path.normpath(p))
             or print(f"[Skip]   Not found: {p}") is None and os.path.isfile(os.path.normpath(p))]
    valid = [os.path.normpath(p) for p in paths if os.path.isfile(os.path.normpath(p))]
    skipped = [p for p in paths if not os.path.isfile(os.path.normpath(p))]
    for p in skipped:
        print(f"[Skip]   File not found: {p}")

    if len(valid) < 2:
        sys.exit("[Error]  Need at least 2 valid files to combine.")

    layout = _detect_layout(valid[0])
    if not output:
        output = os.path.join(os.path.dirname(os.path.abspath(valid[0])), "combined.csv")
    print(f"\nDetected layout : {layout}")
    print(f"Combining {len(valid)} file(s)...\n")

    if layout == "columns":
        _combine_columns(valid, output, override)
    else:
        _combine_rows(valid, output, override)

# Tooltip text for every labelled field in the GUI.
# Keyed by the settings field name; looked up via _TIPS.get(key, "").

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
        "To narrow the search by parent / child elements, use\n"
        "the \"Add Child\" button to add each child level"
    ),
    "add_child": (
        "Add a child element.\n\n"
        "Paste an HTML tag from Inspect Element.\n\n"
        "To target a specific occurrence, place a number directly before the child element:\n"
        "  <div class=\"row\"> 2<span class=\"value\">text</span>\n"
        "   -> the 2nd span.value inside div.row\n\n"
        "Bare numbers after a parent select by child position:\n"
        "  <div class=\"row\"> 2 3\n"
        "   -> the 3rd child of the 2nd child of div.row\n\n"
        "All of these can be combined and stacked freely."
    ),
    "remove_child": (
        "Remove this child element."
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
        "Format: YYYY MM DD\n\n"
        "Leave blank to search all available snapshots."
    ),
    "to_date": (
        "End of the date range to search.\n"
        "Format: YYYY MM DD\n\n"
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
        "The index of the element(s) whose output become the LABELS in the reformatted file.\n"
        "e.g. 2 will treat element_2 as the label.\n\n"
        "Each pair links a label element index (top row) to its corresponding value element\n"
        "index (bottom row).\n"
        "e.g. for label_elements = 1 2 and value_elements = 3 4, elements 1 and 3 are paired\n"
        "and elements 2 and 4 are paired."
    ),
    "value_elements": (
        "The index of the element(s) whose output become the VALUES in the reformatted file.\n"
        "e.g. 3 will treat element_3 as the value to track.\n\n"
        "Each pair links a label element index (top row) to its corresponding value element\n"
        "index (bottom row).\n"
        "e.g. for label_elements = 1 2 and value_elements = 3 4, elements 1 and 3 are paired\n"
        "and elements 2 and 4 are paired."
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
        "snapshot -> places 0 in the snapshot before the first value\n\n"
        "only effective when result_padding is enabled"
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
    "label_merge": (
        "Merge labels that are the same characters but differ only in case, spaces, or separators.\n\n"
        "e.g. 'value-1', 'Value 1', and 'VALUE_1' are treated as the same label.\n\n"
        "yes -> merge equivalent labels into one row/column\n"
        "no  -> treat each distinct string as a separate label"
    ),
    "label_strip_separators": (
        "Remove '-' and '_' characters from labels before writing them to the output.\n\n"
        "e.g. 'element-1' becomes 'element 1', 'my_value' becomes 'my value'.\n\n"
        "Enabled automatically when label_merge = yes."
    ),
    "label_case": (
        "How to display labels in the reformatted file.\n\n"
        "default    -> no change; use the label exactly as it appears\n"
        "lower      -> convert to lowercase\n"
        "upper      -> convert to UPPERCASE\n"
        "sentence -> Capitalize first letter, rest lowercase"
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
    "end_passes": (
        "After the main run finishes, retry all still-failed snapshots this many times.\n\n"
        "Each end pass makes one attempt per failed snapshot with no retries,\n"
        "separated by the delay interval. Useful for recovering snapshots that\n"
        "failed due to transient errors (timeouts, rate limiting) during the main run.\n\n"
        "0 -> disabled"
    ),
    "threads": (
        "Number of parallel threads for fetching snapshots.\n\n"
        "Has no effect when headless_browser = yes."
    ),
    "pairs_add": (
        "Add a new label/value element pair."
    ),
    "pairs_remove": (
        "Remove the last label/value element pair."
    ),
    "shortcut_clear": (
        "Clear this shortcut."
    ),
    "shortcut_focus_log": (
        "Focuses the output log panel so you can scroll through it or select\n"
        "and copy text without having to click it with the mouse."
    ),
    "always_on_top": (
        "Keep the window on top of all other windows."
    ),
}

# Title/message pairs for every confirmation or error dialog in the GUI and tracker.
# Each entry is a dict with "title" and "message" keys.
# Where the message contains a placeholder, format it at the call site:
#   _DIALOGS[key]["message"].format(n=n)
_DIALOGS = {
    # -- GUI settings dialogs --------------------------------------------------
    "reset_saved": {
        "title":   "Reset to Last Saved",
        "message": "Discard all unsaved changes and reload from settings.txt?",
    },
    "reset_defaults": {
        "title":   "Reset to Defaults",
        "message": (
            "This will clear all settings back to their default values.\n"
            "Settings.txt will not be changed until you click Save Settings.\n\n"
            "Continue?"
        ),
    },
    "load_error_read": {
        "title":   "Load Settings",
        "message": "Could not read file:\n{e}",
    },
    "load_error_invalid": {
        "title":   "Load Settings",
        "message": (
            "This file does not appear to be a valid settings file.\n\n"
            "No recognised settings keys were found."
        ),
    },
    "unsaved_changes": {
        "title":   "Unsaved Changes",
        "message": "You have unsaved changes.\nSave before closing?",
    },
    "cannot_start": {
        "title":   "Cannot Start",
        "message": "{errors}",
    },
    "run_error": {
        "title":   "Error",
        "message": "{msg}",
    },
    # -- Tracker runtime confirmation dialogs ----------------------------------
    "preflight_many_urls": {
        "title":   "Many distinct URLs",
        "message": (
            "This query will match at least {n} distinct URLs.\n"
            "This may be unintentionally broad (e.g. tracking every sub-ID "
            "under a path like /page/1, /page/2, ...).\n"
            "Continue anyway?"
        ),
    },
    "preflight_high_count": {
        "title":   "High request count",
        "message": (
            "This query will fetch roughly {estimate} snapshots.\n"
            "This may take a long time and place heavy load on the archive. "
            "Make sure this is intended.\n"
            "Continue anyway?"
        ),
    },
    "preflight_timeout_urls": {
        "title":   "URL check timed out",
        "message": (
            "The preflight URL check timed out.\n"
            "This likely means the query matches an extremely large number of "
            "distinct URLs.\n"
            "Continue anyway?"
        ),
    },
    "preflight_timeout_count": {
        "title":   "Snapshot count check timed out",
        "message": (
            "The preflight snapshot count check timed out.\n"
            "This likely means the query will fetch an extremely large number "
            "of snapshots.\n"
            "Continue anyway?"
        ),
    },
}

# All error message strings used by the GUI's pre-flight validation and the
# tracker's _error_exit() / sys.exit() calls.
# Use .format(**kwargs) at the call site to fill any {placeholders}.
_ERRORS = {
    # -- GUI pre-flight validation (wayback_gui.pyw _validate) ----------------
    "url_required":            "'URL' is required.",
    "element_required":        "At least one Element must be set.",
    "val_min_gap":             "'Min Gap' must be a number \u2265 0 (got: {val!r}).",
    "val_fallback_candidates": "'Fallback Candidates' must be an integer \u2265 0 (got: {val!r}).",
    "val_end_passes":          "'End Passes' must be an integer \u2265 0 (got: {val!r}).",
    "val_delay":               "'Delay' must be a number (got: {val!r}).",
    "val_retries":             "'Retries' must be an integer \u2265 0 (got: {val!r}).",
    "val_threads":             "'Threads' must be a positive integer (got: {val!r}).",
    "val_reformat_missing":    (
        "'Label Elements' and 'Value Elements' must both be set "
        "when Reformat = yes."
    ),
    "val_reformat_mismatch":   (
        "'Label Elements' has {label_count} slot(s) but "
        "'Value Elements' has {value_count}. They must have the same count."
    ),
    "val_reformat_overlap":    (
        "'Label Elements' and 'Value Elements' must differ "
        "(slot {slot} appears in both)."
    ),
    "val_reformat_not_int":    (
        "'Label Elements' and 'Value Elements' must contain "
        "integer slot numbers."
    ),

    # -- Tracker settings validation (wayback_element_tracker.py) -------------
    "parse_element": (
        "[Error] Could not parse {label} in settings.txt.\n"
        "        Paste the full HTML tag, e.g.:\n"
        "        element_{slot} = <p class=\"rbx-lead\" title=\"28,760,666\">28M+</p>"
    ),
    "url_missing":               "[Error] 'url' is missing from settings.txt",
    "frequency_invalid":         "[Error] 'frequency' must be one of: {values}",
    "sample_from_invalid":       "[Error] 'sample_from' must be 'start', 'middle', or 'end'",
    "csv_layout_invalid":        "[Error] 'csv_layout' must be 'columns' or 'rows'",
    "year_digits_invalid":       "[Error] 'year_digits' must be '2' or '4'",
    "date_format_invalid":       "[Error] '{field}' must be in YYYYMMDD format (e.g. 20231105), got: {val!r}",
    "min_gap_invalid":           "[Error] 'min_gap' must be a number >= 0, e.g. 0.5",
    "fallback_candidates_invalid": "[Error] 'fallback_candidates' must be an integer >= 0",
    "end_passes_invalid":        "[Error] 'end_passes' must be an integer >= 0",
    "sort_invalid":              "[Error] 'sort' must be 'alphabet', 'reverse', or 'unsorted'.",
    "reformat_missing": (
        "[Error] 'label_elements' and 'value_elements' must both be "
        "set when reformat = yes.\n"
        "        Use space-separated slot numbers for multiple pairs, e.g.:\n"
        "        label_elements = 1 2\n"
        "        value_elements = 3 4"
    ),
    "reformat_not_int":          "[Error] 'label_elements' and 'value_elements' must be integers matching element_N slot numbers.",
    "reformat_mismatch":         "[Error] 'label_elements' has {label_count} slot(s) but 'value_elements' has {value_count}. They must have the same count.",
    "reformat_overlap":          "[Error] label_elements and value_elements must be different (slot {slot} appears in both).",
    "extract_invalid": (
        "[Error] extract_{slot} = '{extract}' is not recognised.\n"
        "        Use 'text', a known attribute, or a data-* attribute."
    ),
    "element_missing": (
        "[Error] At least one element_N must be set in settings.txt\n"
        "        (e.g. element_1 = <p class=\"...\">...</p>)."
    ),
    "threads_invalid":           "[Error] 'threads' must be a positive integer.",
    "zero_fill_invalid":         "[Error] 'zero_fill' must be 'no', 'adjacent', or 'snapshot'.",
    "label_case_invalid":        "[Error] 'label_case' must be 'default', 'lower', 'upper', or 'sentence'.",
    "split_output_invalid":      "[Error] 'split_output' must be 'no', 'files', or 'merged'.",
    "merged_meta_invalid":       "[Error] 'merged_meta' must be 'interleaved' or 'grouped'.",
    "collision_priority_invalid": "[Error] 'collision_priority' must be 'time' or 'filter'.",
    "playwright_missing": (
        "[Error] 'headless_browser = yes' requires Playwright.\n"
        "        Install it with:\n"
        "          pip install playwright"
    ),
    "chromium_failed":           "[Error]  Failed to launch Chromium: {error}",
    "no_snapshots":              "No snapshots to process.",
    "preflight_aborted_urls":    "[Aborted] Re-check your filter settings and try again.",
    "preflight_aborted_count":   "[Aborted] Re-check your settings and try again.",
    "preflight_aborted_timeout_urls":  "[Aborted] Re-check your filter settings and try again.",
    "preflight_aborted_timeout_count": "[Aborted] Re-check your settings and try again.",
    "cdx_failed":                "[CDX]    Query failed after {retries} attempts: {error}",
    "preflight_failed":          "[CDX]    Preflight check failed: {error}",
}

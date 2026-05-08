# Wayback Element Tracker

### ABOUT

Fetches archived snapshots of a webpage from the Wayback Machine, extracts a
specific HTML element's value from each one, and saves the results to a CSV file.

Requirements before running (type into command prompt):  
&nbsp; &nbsp; &nbsp;`pip install -r requirements.txt`

#### Usage:  
&nbsp; &nbsp; &nbsp;Double click or run in command prompt:  
&nbsp; &nbsp; &nbsp;`python wayback_element_tracker.pyw`
<br>
<br>
### URL

`url`  
The full URL of the page to track, e.g. `https://www.example.com`  
<br>
<br>
`filter_any`, `filter_all`  
Controls which archived URL variants are included. URLs must satisfy the filter(s) or will be skipped.  
&nbsp; &nbsp; &nbsp;`filter_any` -> URL must match at least ONE filter  
&nbsp; &nbsp; &nbsp;`filter_all` -> URL must match EVERY filter

Both fields support the same filter syntax and can be used independently or together.

| Filter_* | Behavior |
|---|---|
| (blank) | match only the exact URL, no variants |
| `*` | include all URL variants |
| `/subpage` | match URLs where `/subpage` appears at the end of the path, e.g. `example.com/subpage` |
| `key=value` | match only URLs containing `key=value` as a query parameter, e.g. `example.com?lang=en` |
| `[filter]*` | substring match anywhere in the URL, e.g. `key=*` matches both `key=1` and `key=2`; `/subpage*` matches `example.com/subpage-a` and `example.com/subpage-b` |
| `![filter]` | exclude instead of include; using only exclude filters will fetch all URL variants (works with all of the above) |

Multiple filters are separated by spaces, e.g. `/images` `key=value` `!page=2`  
<br>
<br>
`case_sensitive`  
Whether filter matching is case sensitive (`yes` / `no`)  
<br>
<br>
`match_child_paths`  
Whether url path filters (e.g. `/subpage`) also match child pages deeper in the url.

| match_child_paths | Behavior |
|---|---|
| `yes` | `/subpage` also matches `example.com/subpage/child`, `example.com/subpage/child/page`, etc. |
| `no` | `/subpage` matches only `example.com/subpage` exactly |

Note: substring filters like `/subpage*` always match child paths regardless of this setting.
<br>
<br>
### HTML ELEMENTS

`element_1` ... `element_5`  
The HTML element(s) to be tracked, e.g. `<p class="paragraph">text</p>`  
Paste HTML tag from Inspect Element for each one.  
<br>
<br>
`extract_1` ... `extract_5`  
What to extract from the element:

| extract_* | Extracts |
|---|---|
| `text` | visible text inside the element |
| `title` | `title="..."` attribute |
| `href` | `href="..."` attribute (links) |
| `src` | `src="..."` attribute (images, scripts) |
| `value` | `value="..."` attribute (inputs) |
| `content` | `content="..."` attribute (meta tags) |
| `alt` | `alt="..."` attribute (image descriptions) |
| `placeholder` | `placeholder="..."` attribute (input hints) |
| `datetime` | `datetime="..."` attribute (time elements) |
| `action` | `action="..."` attribute (forms) |
| `data-*` | any custom data attribute, e.g. `data-count`, `data-value` |

If a selector matches multiple elements on the page, all of them are captured. In the CSV they appear as separately numbered columns or rows, e.g. `selector [1] (text)`, `selector [2] (text)`, etc.

A parent element can be entered before the target element, separated by spaces,  
e.g. `<div class="paragraph1">` `<span class="paragraph2">text</span>` will target `span.paragraph2` elements that are only inside a `div.paragraph1` element.

To target a specific occurrence of the child element, place a number directly before it,  
e.g. `<div class="paragraph1">` `2<span class="paragraph2">text</span>` grabs the 2nd `span.paragraph2` within `div.paragraph1`.  

Placing bare numbers after the parent without a child element broadens the selection to all child elements,  
e.g. `<div class="paragraph1"> 2 3` simply grabs the 3rd child element of the 2nd child element of `div.paragraph1`

All of these can be combined and stacked freely.
<br>
<br>
### DATE RANGE

`from_date`, `to_date`  
Format: `YYYYMMDD`. Leave blank to search all available snapshots.
<br>
<br>
### SNAPSHOT FREQUENCY

`frequency`  
Frequency of snapshots to check:  
&nbsp; &nbsp; &nbsp;`all` / `hourly` / `daily` / `weekly` / `monthly` / `yearly`  
<br>
<br>
`sample_from`  
Which snapshot to pick within each frequency period:

| sample_from | Description |
|---|---|
| `start` | the snapshot closest to the START of the period |
| `middle` | the snapshot closest to the MIDDLE of the period |
| `end` | the snapshot closest to the END of the period |

Has no effect when `frequency = all`.  
<br>
<br>
`collision_priority`  
When multiple URL variants have snapshots in the same time period, determines which one is preferred:
| collision_priority | Description |
|---|---|
| `time` | the variant whose timestamp is closest to the `sample_from` anchor wins |
| `filter` | earlier listed `filter_any` filters take priority over later ones |

Has no effect when no URL variants are tracked, or when `split_output = files`.
<br>
<br>
### DATE & TIME FORMAT

`convention`

| convention | Format |
|---|---|
| `us` | month first (`November 5, 2023`) |
| `european` | day first (`5 November 2023`) |

<br>
<br>

`date_style`

| date_style | Example |
|---|---|
| `long` | `November 5, 2023` / `5 November 2023` |
| `short` | `Nov 5, 2023` / `5 Nov 2023` |
| `numeric` | `11/5/2023` / `5/11/2023` |

<br>
<br>

`year_digits`

| year_digits | Example |
|---|---|
| `4` | `2023` |
| `2` | `23` |

<br>
<br>


`date_padding`

| date_padding | Example |
|---|---|
| `yes` | `11/05/2023` |
| `no` | `11/5/2023` |

<br>
<br>


`time_format`

| time_format | Example |
|---|---|
| `12h` | `2:30 PM` |
| `24h` | `14:30` |

<br>
<br>


`time_padding`

| time_padding | Example |
|---|---|
| `yes` | `06:50` |
| `no` | `6:50` |

<br>
<br>


`show_seconds`  
Show seconds in the time? (`yes` / `no`)
<br>
<br>
### OUTPUT
After each run, a `.log` file is saved alongside the CSV with the same base name (e.g. `wayback_results.log`). Each new program execution is added to the end of the log file, which will track the output up to the last 10 runs.  
<br>
<br>
`output`  
CSV file name  
<br>
<br>
`file_override`  
Whether to overwrite the output file(s) if it already exists (`yes` / `no`)  
If `no`, an incrementing counter is added instead,  
e.g `wayback_results.csv` -> `wayback_results_1.csv` -> `wayback_results_2.csv` -> ...  
<br>
<br>
`csv_layout`

<table>
<tr><th>csv_layout</th><th>Layout</th><th>Example</th></tr>
<tr><td>`columns`</td><td>each attribute is a column, each snapshot is a row</td><td>

| date | time | element | url | error |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

</td></tr>
<tr><td>`rows`</td><td>each attribute is a row, each snapshot is a column</td><td>

| date | Jan 1 | Feb 1 | ... |
|---|---|---|---|
| elem | value | value | ... |

</td></tr>
</table>

The `url` column contains the full Wayback Machine URL of each snapshot.
The `error` column is blank on success, or contains the failure reason (e.g. `timeout`, `HTTP 404`).  

When an element cannot be extracted, its cell in the CSV is left empty. The console output distinguishes two cases:

| Console output | Meaning |
|---|---|
| `(no element)` | the element was not found anywhere on the page |
| `(blank)` | the element was found, but the extracted attribute or text was empty |

<br>
<br>

`result_padding`  
Insert blank rows/columns in the CSV file for time periods that had no archived snapshots

<table><tr><td>

`yes`

| Jan 1 | Feb 1 | Mar 1 | ... |
|---|---|---|---|
| value | | value | ... |

</td><td>

`no`

| Jan 1 | Mar 1 | ... |
|---|---|---|
| value | value | ... |

</td></tr></table>

Has no effect when `frequency = all`.  
<br>
<br>
`split_output`

| split_output | Behavior |
|---|---|
| `no` | all variants written into one file; collisions resolved by `collision_priority` |
| `files` | one output file per URL variant or filter |
| `merged` | one output file containing all filter groups separately |

When `split_output = files` or `split_output = merged`, it follows this structure:  
`filter_any`  
&nbsp; &nbsp; &nbsp;Substring filters (e.g. `/subpage*`, `*`) produce one group per distinct URL matched by that filter  
&nbsp; &nbsp; &nbsp;Non substring filters (e.g. `/subpage`, `sort=new)` have all matching variants merged into their respective groups  
`filter_all`  
&nbsp; &nbsp; &nbsp;Substring filters also produce one group per distinct URL.  
&nbsp; &nbsp; &nbsp;`filter_all` is not used in non substring cases since its logic means every result already matched both filters.  
When either field has only `!` filters, the split falls back to one file per distinct URL.  
<br>
<br>
`show_month`  
Show the month in the output CSV? (`yes` / `no`)  
<br>
<br>
`show_day`  
Show the day in the output CSV? (`yes` / `no`)  
<br>
<br>
`show_year`  
Show the year in the output CSV? (`yes` / `no`)  
<br>
<br>
`show_time`  
Show the time in the output CSV? (`yes` / `no`)
<br>
<br>
### KEYBOARD SHORTCUTS

| Shortcut | Action |
|---|---|
| `Ctrl+Tab` | Next tab |
| `Ctrl+Shift+Tab` | Previous tab |
| `Alt+1` ... `Alt+6` | Jump to tab 1 - 6 |
| `Tab` / `Shift+Tab` | Move focus to next / previous field |
| `Enter` / `Space` | Invoke focused button; open focused dropdown |
| `Ctrl+S` | Save Settings |
| `F5` | Start run |
| `Escape` | Stop run (only while running) |
| `Alt+L` | Focus output log (for scrolling / copying) |
| `Ctrl+Z` | Undo text |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo text |

<br>

### REFORMAT

`reformat`  
Writes an additional `[filename]_reformatted` CSV per raw output file alongside. (`yes` / `no`)  
The reformatted file pairs 2 elements, with one being a label and the other being a value for the label. The reformatting moves each value element into one row (or column) per unique label:

<table><tr><td>

| date | Jan 1 | Feb 1 | ... |
|---|---|---|---|
| elem | label | label | ... |
| elem | value | value | ... |

</td><td>-></td><td>

| date | Jan 1 | Feb 1 | ... |
|---|---|---|---|
| label | value | value | ... |

</td></tr></table>

In cases where there are multiple of the same elements, the label index from a snapshot is paired with value index from the same snapshot.  
<br>
<br>
`label_elements`  
The index of the element(s) whose output become the LABELS in the reformatted file,  
e.g. `2` will treat the `element_2` element as the label.  
<br>
<br>
`value_elements`  
The index of the element(s) whose output become the VALUES (the changing data across snapshots) in the reformatted file,  
e.g. `3` will treat the `element_3` element as the value to track

For both fields, multiple indexes can be entered, separated by spaces. The index of the label element will be paired with the index of the value element,  
e.g. for label elements `1` `2` and value elements `3` `4`, elements `1` and `3` will be paired and elements `2` and `4` will be paired.  
<br>
<br>
`sort`  
How to order the label rows / columns in the reformatted file:

| sort | Description |
|---|---|
| `unsorted` | labels appear in first-seen order |
| `alphabet` | alphabetical A-Z (case insensitive) |
| `reverse` | alphabetical Z-A (case insensitive) |

<br>
<br>

`zero_fill`  
When a label first appears partway through the timeline, places a `0` before its first value.

| zero_fill | Description |
|---|---|
| `no` | disabled |
| `adjacent` | places `0` in the cell DIRECTLY before the first value |
| `snapshot` | places `0` in the SNAPSHOT before the first value (only effective when `result_padding` enabled) |

<br>
<br>

`fill_first`  
Also place a `0` before labels whose first value appears at the very start of the timeline. (`yes` / `no`)  
<br>
<br>
`merged_meta`  
Controls where snapshot URLs and errors appear in the reformatted file when `split_output = merged`. Has no effect otherwise.

| merged_meta | Description |
|---|---|
| `grouped` | all data rows for all groups appear first, then all `url (suffix)` rows, then all `error (suffix)` rows at the bottom |
| `interleaved` | each filter has a group label, then `url (suffix)`, `error (suffix)`, then that filter's data rows |

<br>

### FETCH MODE
`headless_browser`  
Use a headless Chromium browser to fetch every snapshot instead of a plain HTTP request. (`yes` / `no`)  

Enable this when the regular fetch consistently returns blank or missing values that are visible when loading the page in a real browser. This executes each page's JavaScript fully before extracting elements, which is needed when a site populates element content with Javascript.  

Note: significantly slower and resource intensive than the default method. If the relevant Javascript API calls were not archived at the time of the snapshot, it falls back to the live API and returns current data instead of historical data. Chromium (~300MB) is downloaded automatically on first use.
<br>
<br>
### ADVANCED

`min_gap`  
Minimum gap between 2 consecutive selected snapshots, as a fraction of the
frequency period. Snapshots closer together than this are compared and the
one farther from its anchor is discarded.

| min_gap | Description |
|---|---|
| `0.5` | half the period, e.g. ~15 days for monthly, 12 hours for daily |
| `0` | disabled |

Has no effect when `frequency = all`.  
<br>
<br>
`delay`  
Seconds to wait between retry attempts and between CDX query retries.  
<br>
<br>
`retries`  
How many times to retry a failing snapshot or CDX query before giving up.  
Note: HTTP 404 and 403 responses are not retried, they fail immediately.  
<br>
<br>
`fallback_candidates`  
When a snapshot fails, how many closest snapshots from the same time period to try before giving up.  
Candidates are capped by `min_gap`: any snapshot further than `min_gap` away from the selected snapshot is excluded.  
Has no effect when `frequency = all`.  
<br>
<br>
`threads`  
Number of parallel threads for fetching snapshots. Has no effect when `headless_browser = yes`
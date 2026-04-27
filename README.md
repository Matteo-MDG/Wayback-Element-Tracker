# Wayback Element Tracker

### ABOUT

Fetches archived snapshots of a webpage from the Wayback Machine, extracts a
specific HTML element's value from each one, and saves the results to a CSV file.

Requirements before running (type into command prompt):  
&nbsp; &nbsp; &nbsp;`pip install -r requirements.txt`

#### Usage:  
&nbsp; &nbsp; &nbsp;Edit settings.txt, then run in command prompt:  
&nbsp; &nbsp; &nbsp;`python wayback_element_tracker.py`
<br>
<br>
### URL

`url`  
The full URL of the page to track, e.g. `https://www.example.com`

`url_filter`  
Controls which archived URLs are included in the search (case sensitive).  
&nbsp; &nbsp; &nbsp;_(blank)_&nbsp; &nbsp; &nbsp; &nbsp; -> match only the exact URL, no variants  
&nbsp; &nbsp; &nbsp;`*` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> include all URL variants  
&nbsp; &nbsp; &nbsp;`/subpage` &nbsp; -> include only URLs whose path contains `/subpage`,  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; e.g. `/images` matches `example.com/images` and `example.com/images/search`  
&nbsp; &nbsp; &nbsp;`key=value` -> include only URLs where `key=value` is a query parameter,  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; e.g. `example.com?lang=en`  
&nbsp; &nbsp; &nbsp;`<filter>*` -> substring match, e.g. `key=*` matches both `key=1` and `key=2`, `images*` matches both `/images` and `key=images`  
&nbsp; &nbsp; &nbsp;`!<filter>` -> exclude instead of include (works with all of the above)  
Multiple filters are separated by spaces, e.g. `/images key=value` or `* !page=2 !page=3`
<br>
<br>
### HTML ELEMENTS

`element_1` ... `element_5`  
The HTML element(s) to be tracked, e.g. `<p class="paragraph">text</p>`  
Paste HTML tag from Inspect Element for each one.  

`extract_1` ... `extract_5`  
What to extract from the element:  
&nbsp; &nbsp; &nbsp;`text` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> visible text inside the element  
&nbsp; &nbsp; &nbsp;`title` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> `title="..."` attribute  
&nbsp; &nbsp; &nbsp;`href` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> `href="..."` attribute (links)  
&nbsp; &nbsp; &nbsp;`src` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> `src="..."` attribute (images, scripts)  
&nbsp; &nbsp; &nbsp;`value` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> `value="..."` attribute (inputs)  
&nbsp; &nbsp; &nbsp;`content` &nbsp; &nbsp; &nbsp; &nbsp;-> `content="..."` attribute (meta tags)  
&nbsp; &nbsp; &nbsp;`alt` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> `alt="..."` attribute (image descriptions)  
&nbsp; &nbsp; &nbsp;`placeholder` -> `placeholder="..."` attribute (input hints)  
&nbsp; &nbsp; &nbsp;`datetime` &nbsp; &nbsp; &nbsp;-> `datetime="..."` attribute (time elements)  
&nbsp; &nbsp; &nbsp;`action` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;-> `action="..."` attribute (forms)  
&nbsp; &nbsp; &nbsp;`data-*` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;-> any custom data attribute, e.g. data-count, data-value  
If a selector matches multiple elements on the page, all of them are captured.

In cases where the element name is vague, enter the parent element and then the target element, e.g. `<div class="paragraph1">` `<span class="paragraph2">text</span>` will target "span.paragraph2" elements that are only inside a "div.paragraph1" element.

To target a specific occurrence of the child element, place a number directly before it, e.g. `<div class="paragraph1">` `2<span class="paragraph2">text</span>` grabs the 2nd span.paragraph2 within div.paragraph1. Placing bare numbers after the parent without a child element boardens the selection to all child elements, e.g. `<div class="paragraph1"> 2 3` simply grabs the 3rd child element of the 2nd child element of "div.paragraph1"

Both of these can be combined and stacked freely.
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

`sample_anchor`  
Which snapshot to pick within each frequency period:  
&nbsp; &nbsp; &nbsp;`start` ->  the snapshot closest to the START of the period  
&nbsp; &nbsp; &nbsp;`end` &nbsp; &nbsp;->  the snapshot closest to the END of the period
<br>
<br>
### DATE & TIME FORMAT

`convention`  
&nbsp; &nbsp; &nbsp;`us` &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; -> month first  (November 5, 2023)  
&nbsp; &nbsp; &nbsp;`european` -> day first    (5 November 2023)

`date_style`  
&nbsp; &nbsp; &nbsp;`long` &nbsp; &nbsp; &nbsp;-> November 5, 2023  /  5 November 2023  
&nbsp; &nbsp; &nbsp;`short` &nbsp; &nbsp;-> Nov 5, 2023       /  5 Nov 2023  
&nbsp; &nbsp; &nbsp;`numeric` -> 11/5/2023         /  5/11/2023

`year_digits`  
&nbsp; &nbsp; &nbsp;`4` ->  2023  
&nbsp; &nbsp; &nbsp;`2` ->  23

`date_padding`  
&nbsp; &nbsp; &nbsp;`yes` ->  11/05/2023  
&nbsp; &nbsp; &nbsp;`no` &nbsp; ->  11/5/2023

`time_format`  
&nbsp; &nbsp; &nbsp;`12h` ->  2:30 PM  
&nbsp; &nbsp; &nbsp;`24h` ->  14:30

`time_padding`  
&nbsp; &nbsp; &nbsp;`yes` ->  06:50  
&nbsp; &nbsp; &nbsp;`no` &nbsp; ->  6:50

`show_seconds`  
Show seconds in the time? (`yes` / `no`)
<br>
<br>
### OUTPUT

`output`  
CSV file name

`csv_layout`  
&nbsp; &nbsp; &nbsp;`columns` ->  each attribute is a column, each snapshot is a row  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; date | time | element | extract | url | error  
&nbsp; &nbsp; &nbsp;`rows` &nbsp; &nbsp; &nbsp;->  each attribute is a row, each snapshot is a column  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; date &nbsp; &nbsp;| Jan 1 | Feb 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; elem &nbsp; &nbsp;| value | value | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; extract | alt &nbsp; | alt &nbsp; | ...  

The extract type (e.g. `alt`, `text`, `src`) is always written to the row or
column immediately after the element's value, as a reminder of what was extracted.

`result_padding`  
Insert blank rows/columns in the CSV file for time periods that had no archived snapshots  
&nbsp; &nbsp; &nbsp;`yes` -> Jan 1 | Feb 1 | Mar 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;value | &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; | value | ...  

&nbsp; &nbsp; &nbsp;`no` &nbsp; -> Jan 1 | Mar 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;value | value | ...

`show_month`  
Show the month in the output CSV? (`yes` / `no`)

`show_day`  
Show the day in the output CSV? (`yes` / `no`)

`show_year`  
Show the year in the output CSV? (`yes` / `no`)

`show_time`  
Show the time in the output CSV? (`yes` / `no`)
<br>
<br>
### ADVANCED

`min_gap`  
Minimum gap between 2 consecutive selected snapshots, as a fraction of the
frequency period. Snapshots closer together than this are compared and the
one farther from its anchor is discarded.  
&nbsp; &nbsp; &nbsp;`0.5` ->  half the period (~15 days for monthly, ~12 hours for daily)  
&nbsp; &nbsp; &nbsp;`0`&nbsp; &nbsp; ->  disabled

`delay`  
Seconds to wait between retry attempts and between CDX query retries.

`retries`  
How many times to retry a failing snapshot or CDX query before giving up.

`end_passes`  
How many times to go back at the end of the script and retry all snapshots that failed.

`threads`  
Number of parallel threads for fetching snapshots.
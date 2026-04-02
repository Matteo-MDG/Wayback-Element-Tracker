# Wayback Element Tracker

### ABOUT

Fetches archived snapshots of a webpage from the Wayback Machine, extracts a
specific HTML element's value from each one, and saves the results to a CSV file.

Requirements before running (type into command prompt):
&#x20;   `pip install -r requirements.txt`

Usage:
&#x20;   Edit settings.txt, then run in command prompt:
&#x20;   `python wayback_element_tracker.py`


### URL

`url`  
The full URL of the page to track, e.g. `https://www.example.com`


### HTML ELEMENTS

`element_1` ... `element_5`  
The HTML element(s) to be tracked, e.g. `<p class="paragraph">text</p>`  
Paste HTML tag from Inspect Element for each one.  

`extract_1` ... `extract_5`  
What to extract from the element:  
&#x20;   `text`        -> visible text inside the element  
&#x20;   `title`       -> title="..." attribute  
&#x20;   `href`        -> href="..." attribute (links)  
&#x20;   `src`         -> src="..." attribute (images, scripts)  
&#x20;   `value`       -> value="..." attribute (inputs)  
&#x20;   `content`     -> content="..." attribute (meta tags)  
&#x20;   `alt`         -> alt="..." attribute (image descriptions)  
&#x20;   `placeholder` -> placeholder="..." attribute (input hints)  
&#x20;   `datetime`    -> datetime="..." attribute (time elements)  
&#x20;   `action`      -> action="..." attribute (forms)  
&#x20;   `data-\*`      -> any custom data attribute, e.g. data-count, data-value  
If a selector matches multiple elements on the page, all of them are captured.


### DATE RANGE

`from_date`, `to_date`  
Format: `YYYYMMDD`. Leave blank to search all available snapshots.


### SNAPSHOT FREQUENCY

`frequency`  
Frequency of snapshots to check:  
&#x20;   `all / hourly / daily / weekly / monthly / yearly`

`sample_anchor`  
Which snapshot to pick within each frequency period:  
&#x20;   `start`  ->  the snapshot closest to the START of the period  
&#x20;   `end `   ->  the snapshot closest to the END of the period


### DATE & TIME FORMAT

`convention`  
Date order convention:  
&#x20;   `us`       -> month first  (November 5, 2023)  
&#x20;   `european `-> day first    (5 November 2023)

`date_style`  
&#x20;   `long`    -> November 5, 2023  /  5 November 2023  
&#x20;   `short`   -> Nov 5, 2023       /  5 Nov 2023  
&#x20;   `numeric` -> 11/5/2023         /  5/11/2023

`year_digits`  
&#x20;   4  ->  2023  
&#x20;   2  ->  23

`date_padding`  
Show leading zeros on day/month?  
&#x20;   yes  ->  11/05/2023  
&#x20;   no   ->  11/5/2023

`time_format`  
&#x20;   12h  ->  2:30 PM  
24h  ->  14:30

`time_padding`  
Show leading zero in 24h time format?  
&#x20;   yes  ->  06:50  
&#x20;   no   ->  6:50

`show_seconds`  
Show seconds in the time? (`yes` / `no`)


### OUTPUT

`output`  
CSV file name

`csv_layout`  
&#x20;   `columns`  ->  each snapshot is a row, each attribute is a column  
&#x20;               date | time | element | url | error  
&#x20;   `rows`     ->  each snapshot is a column, each attribute is a row  
&#x20;               date | Jan 1 | Feb 1 | ...
&#x20;               elem | value | value | ...

`show_month`  
Show the month in the output CSV? (`yes` / `no`)

`show_day`  
Show the day in the output CSV? (`yes` / `no`)

`show_year`  
Show the year in the output CSV? (`yes` / `no`)

`show_time`  
Show the time in the output CSV? (`yes` / `no`)


### ADVANCED

`min_gap`  
Minimum gap between 2 consecutive selected snapshots, as a fraction of the
frequency period. Snapshots closer together than this are compared and the
one farther from its anchor is discarded.

&#x20;   `0.5`  ->  half the period (~15 days for monthly, ~12 hours for daily)  
&#x20;   `0`    ->  disabled

`delay`  
Seconds to wait between retry attempts and between CDX query retries.

`retries`  
How many times to retry a failing snapshot or CDX query before giving up.

`end_passes`  
How many times to go back at the end of the script and retry all snapshots that failed.

`threads`  
Number of parallel threads for fetching snapshots.
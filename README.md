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

`url_variants`  
Also search all URL variants sharing the same prefix. It is equivalent to appending `*` to the URL in the Wayback Machine. (`yes` / `no`)
<br>
<br>
### HTML ELEMENTS

`element_1` ... `element_5`  
The HTML element(s) to be tracked, e.g. `<p class="paragraph">text</p>`  
Paste HTML tag from Inspect Element for each one.  

`extract_1` ... `extract_5`  
What to extract from the element:  
&nbsp; &nbsp; &nbsp;`text`        -> visible text inside the element  
&nbsp; &nbsp; &nbsp;`title`       -> `title="..."` attribute  
&nbsp; &nbsp; &nbsp;`href`        -> `href="..."` attribute (links)  
&nbsp; &nbsp; &nbsp;`src`         -> `src="..."` attribute (images, scripts)  
&nbsp; &nbsp; &nbsp;`value`       -> `value="..."` attribute (inputs)  
&nbsp; &nbsp; &nbsp;`content`     -> `content="..."` attribute (meta tags)  
&nbsp; &nbsp; &nbsp;`alt`         -> `alt="..."` attribute (image descriptions)  
&nbsp; &nbsp; &nbsp;`placeholder` -> `placeholder="..."` attribute (input hints)  
&nbsp; &nbsp; &nbsp;`datetime`    -> `datetime="..."` attribute (time elements)  
&nbsp; &nbsp; &nbsp;`action`      -> `action="..."` attribute (forms)  
&nbsp; &nbsp; &nbsp;`data-*`      -> any custom data attribute, e.g. data-count, data-value  
If a selector matches multiple elements on the page, all of them are captured.
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
&nbsp; &nbsp; &nbsp;`all / hourly / daily / weekly / monthly / yearly`

`sample_anchor`  
Which snapshot to pick within each frequency period:  
&nbsp; &nbsp; &nbsp;`start`  ->  the snapshot closest to the START of the period  
&nbsp; &nbsp; &nbsp;`end`    ->  the snapshot closest to the END of the period
<br>
<br>
### DATE & TIME FORMAT

`convention`  
&nbsp; &nbsp; &nbsp;`us`       -> month first  (November 5, 2023)  
&nbsp; &nbsp; &nbsp;`european `-> day first    (5 November 2023)

`date_style`  
&nbsp; &nbsp; &nbsp;`long`    -> November 5, 2023  /  5 November 2023  
&nbsp; &nbsp; &nbsp;`short`   -> Nov 5, 2023       /  5 Nov 2023  
&nbsp; &nbsp; &nbsp;`numeric` -> 11/5/2023         /  5/11/2023

`year_digits`  
&nbsp; &nbsp; &nbsp;`4`  ->  2023  
&nbsp; &nbsp; &nbsp;`2`  ->  23

`date_padding`  
&nbsp; &nbsp; &nbsp;`yes`  ->  11/05/2023  
&nbsp; &nbsp; &nbsp;`no`   ->  11/5/2023

`time_format`  
&nbsp; &nbsp; &nbsp;`12h`  ->  2:30 PM  
&nbsp; &nbsp; &nbsp;`24h`  ->  14:30

`time_padding`  
&nbsp; &nbsp; &nbsp;`yes`  ->  06:50  
&nbsp; &nbsp; &nbsp;`no`   ->  6:50

`show_seconds`  
Show seconds in the time? (`yes` / `no`)
<br>
<br>
### OUTPUT

`output`  
CSV file name

`csv_layout`  
&nbsp; &nbsp; &nbsp;`columns`  ->  each snapshot is a row, each attribute is a column  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;date | time | element | url | error  
&nbsp; &nbsp; &nbsp;`rows`     ->  each snapshot is a column, each attribute is a row  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;date | Jan 1 | Feb 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;elem | value | value | ...

`result_padding`  
Insert blank rows in the CSV file for time periods that had no archived snapshots, so
the output covers every period continuously between the first and last result.  
&nbsp; &nbsp; &nbsp;`yes`  -> &nbsp; &nbsp; &nbsp; Jan 1 | Feb 1 | Mar 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;value | &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; | value | ...  

&nbsp; &nbsp; &nbsp;`no`   -> &nbsp; &nbsp; &nbsp; &nbsp; Jan 1 | Mar 1 | ...  
&nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp; &nbsp;value | value | ...

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
&nbsp; &nbsp; &nbsp;`0.5`  ->  half the period (~15 days for monthly, ~12 hours for daily)  
&nbsp; &nbsp; &nbsp;`0`    ->  disabled

`delay`  
Seconds to wait between retry attempts and between CDX query retries.

`retries`  
How many times to retry a failing snapshot or CDX query before giving up.

`end_passes`  
How many times to go back at the end of the script and retry all snapshots that failed.

`threads`  
Number of parallel threads for fetching snapshots.
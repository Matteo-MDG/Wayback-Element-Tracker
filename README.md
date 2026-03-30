Wayback Element Tracker
--- ABOUT -----------------------------------------------------------------------------------------------
Fetches archived snapshots of a webpage from the Wayback Machine, extracts a
specific HTML element's value from each one, and saves the results to a CSV file.

Requirements (type into command prompt):
    pip install requests beautifulsoup4 lxml

Usage:
    Edit settings.txt, then run in command prompt:
    python wayback_scraper.py

--- URL -------------------------------------------------------------------------------------------------
*url*
The full URL of the page to track.
e.g. https://www.example.com

--- HTML ELEMENTS ---------------------------------------------------------------------------------------
*element_1* ... *element_5*
Up to 5 elements can be tracked. Paste HTML tag from Inspect Element for each one.
e.g. <p class="paragraph">text</p>

*extract_1* ... *extract_5*
What to extract from the element:
  text        -> visible text inside the element
  title       -> title="..." attribute
  href        -> href="..." attribute (links)
  src         -> src="..." attribute (images, scripts)
  value       -> value="..." attribute (inputs)
  content     -> content="..." attribute (meta tags)
  alt         -> alt="..." attribute (image descriptions)
  placeholder -> placeholder="..." attribute (input hints)
  datetime    -> datetime="..." attribute (time elements)
  action      -> action="..." attribute (forms)
  data-*      -> any custom data attribute, e.g. data-count, data-value
If a selector matches multiple elements on the page, all of them are captured.

--- DATE RANGE ------------------------------------------------------------------------------------------
*from_date*
*to_date*
Format: YYYYMMDD. Leave blank to search all available snapshots.

--- SNAPSHOT FREQUENCY ----------------------------------------------------------------------------------
*frequency*
Frequency of snapshots to check:
  all / hourly / daily / weekly / monthly / yearly

*sample_anchor*
Which snapshot to pick within each frequency period:
  start  ->  the snapshot closest to the START of the period
  end    ->  the snapshot closest to the END of the period

--- DATE & TIME FORMAT ----------------------------------------------------------------------------------
*convention*
Date order convention:
  us       -> month first  (November 5, 2023)
  european -> day first    (5 November 2023)

*date_style*
  long    -> November 5, 2023  /  5 November 2023
  short   -> Nov 5, 2023       /  5 Nov 2023
  numeric -> 11/5/2023         /  5/11/2023

*year_digits*
  4  ->  2023
  2  ->  23

*date_padding*
Show leading zeros on day/month?
  yes  ->  11/05/2023
  no   ->  11/5/2023

*time_format*
  12h  ->  2:30 PM
  24h  ->  14:30

*time_padding*
Show leading zero in 24h time format?
  yes  ->  06:50
  no   ->  6:50

*show_seconds*
Show seconds in the time? (yes / no)

--- OUTPUT ----------------------------------------------------------------------------------------------
*output*
CSV file name

*csv_layout*
  columns  ->  each snapshot is a row, each attribute is a column
               date | time | element | url | error
  rows     ->  each snapshot is a column, each attribute is a row
               date | Jan 1 | Feb 1 | ...
               elem | value | value | ...

*show_month*
Show the month in the output CSV? (yes / no)

*show_day*
Show the day in the output CSV? (yes / no)

*show_year*
Show the year in the output CSV? (yes / no)

*show_time*
Show the time in the output CSV? (yes / no)

--- ADVANCED --------------------------------------------------------------------------------------------
*min_gap*
Minimum gap between 2 consecutive selected snapshots, as a fraction of the
frequency period. Snapshots closer together than this are compared and the
one farther from its anchor is discarded.
  0.5  ->  half the period (~15 days for monthly, ~12 hours for daily)
  0    ->  disabled

*delay*
Seconds to wait between retry attempts and between CDX query retries.

*retries*
How many times to retry a failing snapshot or CDX query before giving up.

*end_passes*
How many times to go back at the end of the script and retry all snapshots that failed.

*threads*
Number of parallel threads for fetching snapshots.
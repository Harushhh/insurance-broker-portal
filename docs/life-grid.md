# Life grid: the standard file, the review, and what to do when a grid changes

The Life payout grid (what the Rate Checker shows and what the MIS Payout Engine matches Life policies against) is
loaded from **one standard file**, whatever the insurers' own grids look like. Everything else in the portal reads the
stored rows, so a change in an insurer's layout never touches the portal's code: someone turns the new layout into the
standard file. This is the same idea as the Motor and Health CSVs.

## The standard file

One row is one payout row of an insurer's grid. `.xlsx` or `.csv`. The first row names the columns (the stored field
names, like the Motor and Health CSVs). Columns are found **by name**, so their order does not matter and nothing can
shift. On the Life Rate Master, Uploads tab, **Download the template** gives an empty file with a Read me sheet, and
**Export the live grid in this format** gives today's grid in the same shape (an export is itself a valid file).

| Column | Needed | Holds |
|---|---|---|
| `insurer` | yes | The insurer, spelled the same on every row and the same as in the live grid |
| `product` | yes | The plan name as the grid writes it |
| `category`, `variant`, `ppt`, `pt`, `condition` | no | The grid's own wording (`5-10`, `>=10`, `Single Pay`, `15/20`, `Whole Life` ...) |
| `from_date`, `to_date` | no | The dates the row applies between, both included, as `DD/MM/YYYY`, `YYYY-MM-DD` or an Excel date. Empty = no limit on that side; a row with neither applies to any date |
| `payout_yr1`, `payout_deferred`, `payout_yr2` ... `payout_yr5`, `payout_yr6_plus` | at least one, or a rule in `remarks` | The BG payout (the partner payout the Rate Checker shows), in percent |
| `mg_yr1` ... `mg_yr6_plus` | no | The MG of the same years (what the insurer pays the broker); admin pages only |
| `remarks` | no | A written rule that is not a number (for example `Only 7 Pay -1.5%`) |
| `sheet`, `section`, `source_ref` | no | Where the row came from; an empty `source_ref` becomes `Row N` of the file |

An optional second sheet named `Notes` (columns `insurer`, `note`) carries each insurer's terms and conditions.

Rules:

- Payouts are percent numbers: `35.5` means 35.5%. A cell Excel shows as `35.5%` is read as 35.5. An empty cell means no
  payout; `0` means a payout of zero. A written rule goes in `remarks`, not in a payout cell. A row that is only a
  rule and has no number ("Year 1: 30% of 1st year commission") has no payout: put the rule in `remarks`. The live
  grid has a few such rows (Axis Max, LIC), so an export of it loads back.
- Dates are optional and both ends are included: a row with `to_date` 30/11/2026 still applies on 30/11/2026. When a
  rate changes on a date, give the old row a `to_date` and the new row a `from_date`. A note such as "Valid till 30th
  Nov" belongs in `to_date`, not in `remarks`. A grid with no dates behaves exactly as it did before dates existed.
- Keep `ppt` and `pt` as Text (the template does): Excel turns `5-10` into a date otherwise, and the upload refuses that.
- The whole file is checked first. One bad row stops the upload, nothing is saved, and every problem is listed with its
  row number.

## The one-sheet file for a dashboard

The Life Rate Master's **Dashboard** tab downloads the live grid (or the upload picked with `?upload=`) as **one sheet with
no empty cell**. Nothing is stored: it is built from the grid each time, so next month's grid needs no extra work.

One row is one plan in one year, as BG or as MG. Columns: `grid` (the upload's name, so months can be stacked),
`plan_id`, `insurer`, `product`, `plan`, `ppt`, `payout_type` (BG or MG), `payout_year` (Year 1, At 2nd year, Year 2 to
Year 5, Year 6+), `year_order` (1 to 7) and `payout_pct` (35.5 means 35.5%).

How the gaps are handled, with no made-up value anywhere:

- A year the grid gives no payout for has no row, so the 14 mostly-empty payout columns are gone.
- Variant, condition, section and the PT are merged into `plan` (`New Assured Wealth | PT 12`), so insurer + `plan` + `ppt`
  names one plan; `product` is the plain product name to group by.
- Category, remarks and PT as a column are not in the sheet: the grid states them for only some plans, and a column with
  gaps is what the dashboard cannot take. They stay on the Payout rows tab and in its Export Excel.
- A plan the sheet cannot hold is left out and listed on the tab: a plan whose row is only a written rule (no number),
  and a plan the grid gives no PPT (its PPT cell is `-` or empty). Two restatements of what the grid itself says are listed
  as restated: a blank PPT whose Variant is a pay term (Digit Glow Plus: the workbook merges the two cells), and a blank PPT
  whose product name states the range (`ILP 7 - 9 ...`).
- **BG only** leaves the MG rows out. MG is what the insurer pays the broker and is for admins only, so use BG only for any
  dashboard that partners or other non-admins can open.

### Every month

1. Uploads tab: start from the template (or export the live grid and edit it), **Upload and review**.
2. Look at what changed against the live grid, then **Make live**.
3. Dashboard tab: download the sheet.
4. In the dashboard, load the new file in place of last month's, or next to it (the `grid` column keeps the months apart).

## Where the dates are used

- **Rate Checker:** the **Date** box shows only the rows that apply on that day (empty = every row, as on Health Payout
  Rates). Each row shows its own window under the plan name.
- **MIS Payout Engine:** a Life policy is matched to the grid rows whose window contains the policy's `Policy: inception
  date` (day first). A blank or unreadable inception date leaves only the rows with no dates. A policy whose date is
  outside every window is NO MATCH with the windows the grid has in the reason.
- **Edit / Add row** on the Life Rate Master set the dates one row at a time, and the export carries them. There is no
  bulk edit for dates: change many rows by editing the exported file and uploading it back.
- The old workbook upload does not read dates (a note such as "Valid till 30th Nov" stays a remark): load the dates
  through the standard file.

## Loading a grid

1. Life Rate Master, **Uploads**: choose the file and press **Upload and review**.
2. The upload is stored but **not live**. The Rate Checker and the MIS engine keep using the live grid.
3. The review page shows what the upload changes against the live grid: the rows added, removed and changed, a table by
   insurer, and the checks below.
4. **Make live**, or **Discard** (it deletes only an upload that is waiting). The previous grid stays in the list:
   **Make live** on it goes back.

To change a few insurers: export the live grid, edit those rows, upload the file back. The review then shows exactly
the rows you touched.

## Deleting an upload

On the Uploads tab, **Delete** (the red button) removes an earlier upload with all its rows and notes, for good; the file
itself is never kept, so only uploading it again brings it back. It is there to clear out test uploads. The **live** upload
cannot be deleted (its button is greyed out): make another upload live first, then delete the one you no longer want. An
upload that is waiting for review is thrown away with **Discard**. Each delete is written to the audit log.

## What the review checks

| Check | What it means |
|---|---|
| BG payout above its own MG | The partner would get more than the insurer pays the broker: usually a value in the wrong column |
| BG payout moved by 10 points or more | A big jump against the live grid: look at it before going live |
| PPT or PT wording the Rate Checker cannot read | Words with no number ("Five Pay"): the rules read it as "applies to every PPT / PT" |
| Plan mappings / insurer aliases that would stop working | A mapped MIS plan or insurer that this upload no longer has: its policies would end as NO MATCH until the mapping is changed |
| Date windows of one plan that overlap | Two rows of the same plan apply on a common day with different payouts: a policy dated there matches both and ends as MULTIPLE MATCHES. Give the old row an end date before the new row's start date |
| Rows already past their end date / rows that start in the future | Good to know: the MIS engine will not use an ended row for a later policy, and a row that has not started does not match a policy incepted today |
| Rows with no first-year BG payout | The MIS engine prints the first-year payout, so a policy that matches such a row ends as NO MATCH |
| Rows that are a written rule, with no payout number | The Rate Checker shows the rule; the MIS engine cannot use the row (NO MATCH) |
| By insurer | An insurer the upload no longer has, one that lost a quarter of its rows, a new one, or one spelled differently from the live grid |

## When an insurer's grid changes

| What changed | What to do |
|---|---|
| Rates only | Edit the values in the file; the review shows which rows changed |
| Plans added or removed | Add or delete rows in the file |
| A rate changes on a date, or a campaign rate runs for a while | Give the old row a `to_date` and the new row a `from_date` (or a campaign row both). Nothing is deleted, and the review shows the overlap check if the windows clash |
| The layout of the insurer's own file | Nothing changes in the portal: turn the new layout into the standard file, by hand or with a converter script (those live outside the portal, like the Motor and Health grid tools) |
| A new insurer | Add its rows, with the name spelled the same on every row. Then, on Life Mapping, add the MIS spelling(s) and the plan mappings |
| An insurer renamed | Use the new name on every row. The review shows the old name as missing and the new one as new; update the insurer names and plan mappings on Life Mapping |
| A plan renamed | Change `product`. A plan mapping that pointed at the old name is flagged by the review and has to be edited on Life Mapping |
| A new kind of payout that has no column | This needs a developer (a new standard column and a stored field). Until then put the text in `remarks` |

## The old workbook upload

Still on the Uploads tab: it reads the monthly working workbook at fixed positions and goes live at once, unless "show me
what changed first" is ticked. It checks only the first header of each table, so a column inserted inside a table is
read wrongly without a warning; prefer the standard file.

## For developers

- `services/life_standard.py`: reads and checks the file, builds the template (`LifeFileError` lists every problem).
- `services/life_review.py`: `review_upload(upload, live)` builds the review (it writes nothing).
- `LifeGridUpload.is_pending` marks an upload that is stored but not live; `save_life_upload(make_live=False)`,
  `activate_life_upload`, `discard_life_upload` and `delete_life_upload` (`services/life_rates.py`) move it along.
- Views: `life_rate_master_upload_standard`, `life_rate_master_template`, `life_rate_master_review`,
  `life_rate_master_discard`, `life_rate_master_delete_upload` in `views/life_rates.py`.
- `services/life_dashboard.py`: `dashboard_data(upload, include_mg=True)` builds the one-sheet rows and the left-out /
  restated lists, `dashboard_workbook(data)` the Excel; views `life_rate_master_dashboard` (the tab) and
  `life_rate_master_dashboard_download` in `views/life_rates.py`.
- Tests: `insurance/test_life_standard_file.py`, `insurance/test_life_validity_dates.py` and `insurance/test_life_dashboard.py`.
  The longer write-up is in the Life bullets of `CLAUDE.md`.

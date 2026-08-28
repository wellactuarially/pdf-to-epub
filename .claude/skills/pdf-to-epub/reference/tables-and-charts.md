# Tables and Charts

How the `exhibit` strategy turns numeric tables and vector charts into EPUB
content, and how to tune it.

## When to use it

Use `--strategy exhibit` when a meaningful share of the book is tables or
charts: technical manuals, actuarial and statistical texts, financial reports,
scientific papers with data tables.

Use the default `simple` strategy for prose. It is cheaper and there is
nothing for the exhibit passes to do.

```bash
python -m scripts.convert input.pdf output.epub --strategy exhibit
```

## What the simple strategy does with a table

Nothing good. Every text block becomes a `<p>`, so a claim development
triangle arrives as a few hundred loose numbers in whatever order the blocks
were sorted, with the column headings stranded somewhere nearby:

```html
<p>2000  37,246  36,782  35,235  465 - 2,011 - 1,547</p>
<p>1998  15,822  15,822  15,660 0 - 162 - 162</p>
<p>Year Reported Paid Claims at 12/31/08 IBNR Total</p>
```

A vector chart is worse: it has no embedded image to extract, so the plot
disappears entirely and only its axis labels survive, scattered into the
surrounding prose.

## What the exhibit strategy does instead

Two extra passes run before the normal flow.

**Tables** are reconstructed into a cell grid and rendered as real markup —
`<thead>`/`<tbody>`, `colspan` for spanning group headers, `scope` for screen
readers, numeric cells right-aligned with tabular figures. The text blocks the
table consumed are replaced by a placeholder, so the same numbers are not also
emitted as paragraphs.

**Charts** are found as clusters of vector drawing operations, rasterized to
PNG at the configured DPI, and embedded as figures with alt text built from
the labels printed inside the plot. Their axis labels are removed from the
text flow for the same reason.

Everything else — reading order, heading detection, chapter building, endnote
linking — is inherited from `SimpleStrategy` unchanged.

## Why the reconstruction works this way

Generated exhibits are right-aligned: the right edge of a given column is
stable all the way down the table, even when cells are blank. So columns are
derived from the **right edges of numeric data rows**, and three kinds of row
are deliberately excluded from that vote:

- **spanning headers** ("Trend Adjustment" over five year columns) — they
  bridge the gap between columns and would smear the boundaries;
- **column-key rows** (`(1) (2) (3)`) and **year caption rows** — these are
  centred over their columns rather than aligned to them, so letting them vote
  splits every column in two;
- **prose** — justified body text breaks into a couple of wide tokens when
  word spacing stretches, which can look like a two-column row.

A repair pass then adds columns for numeric values that fit nowhere, so a
column filled on only a few rows gets its own column instead of being folded
into a neighbour.

## Rotated pages

Landscape exhibits are usually portrait pages carrying `/Rotate 90`. PDF text
coordinates come back **unrotated** while the page rectangle is already
rotated, so anything sorting by raw `y` reads such a table's rows as columns
and scrambles the page. Both the extractor and the detectors map coordinates
through `page.rotation_matrix` first. If you write a new detector that touches
geometry, do the same.

## Configuration

```json
{
  "table_extraction": {
    "enabled": true,
    "min_rows": 3,
    "min_columns": 2,
    "min_numeric_ratio": 0.35,
    "column_gap": 4.0
  },
  "chart_extraction": {
    "enabled": true,
    "min_area_ratio": 0.10,
    "max_text_density": 3.5,
    "dpi": 150
  }
}
```

| Parameter | Effect | Try when |
|-----------|--------|----------|
| `min_numeric_ratio` | Share of cells that must look numeric | Lower to ~0.15 for word tables; raise to ~0.6 to catch only number grids |
| `min_rows` / `min_columns` | Size floor for a table | Raise to ignore small inline tables |
| `column_gap` | Horizontal gap (pt) separating two cells | Lower if adjacent columns merge; raise if one column splits |
| `max_text_density` | Words per 100pt² above which a region is a table, not a plot | Lower if a dense chart is being skipped; raise if a ruled table is captured as an image |
| `min_area_ratio` | Smallest chart, as a share of the page | Lower to catch small inline figures |
| `dpi` | Chart rendering resolution | Raise for detailed plots; lower to shrink the EPUB |

Set `"enabled": false` on either block to switch that pass off.

## Troubleshooting

**Columns merged into one cell.** Lower `column_gap`. If the table has no
numeric rows at all, the anchors cannot be derived — lower `min_numeric_ratio`
so more rows qualify as data.

**One column split into two.** Raise `column_gap`. Check whether a centred
header row is voting on column positions: `_is_label_row` in
`table_detector.py` is where those rows are excluded, and it may need a new
case for the document's conventions.

**Prose captured as a table.** Raise `min_numeric_ratio`. The prose filter is
`_is_prose_row`, which rejects rows containing a token of more than six words;
tighten that threshold for documents with unusually short paragraphs.

**A table was captured as a chart image.** Raise `max_text_density`.

**A chart was missed.** Lower `min_area_ratio` if it is small, or lower
`max_text_density` if it carries a lot of labels.

**Table missing from the output entirely.** The placeholder mechanism relies
on the detected bounding box overlapping the text blocks it replaces. If the
table renders but the same numbers also appear as paragraphs, the bbox is too
small; if the table is absent, check that `ExhibitStrategy` is actually the
selected strategy.

## Styling

Table CSS ships inside each chapter that contains a table, because the
packaged stylesheet is FROZEN and carries no table rules. Edit
`TABLE_STYLESHEET` in `detectors/table_renderer.py` to change it.

Wide tables are set at a smaller font, scaled by column count, and wrapped in
a horizontally scrolling container. Both matter: readers that support
`overflow-x` let the user pan a 12-column exhibit, and readers that ignore it
still stand a chance of fitting the table on screen.

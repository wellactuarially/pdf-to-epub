# ADAPTABLE: Table markup and styling can be tuned
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Renders reconstructed tables as EPUB-friendly XHTML.

Wide numeric exhibits are the hard case: a 12-column claim development
triangle is wider than a 6-inch e-ink screen at any readable size. A
horizontal scroll container is not an answer — e-ink readers do not scroll
sideways, so the right-hand columns are simply clipped off the page edge and
the exhibit becomes unreadable.

So a table wider than the screen is **split into column groups**, the way a
wide table is handled in print: each group repeats the row-label column and
carries a "columns 7-12 of 12" note, so every number stays reachable and the
rows still line up against their labels.

The rest of the markup follows from the same goal:

* a real ``<table>`` with ``<thead>``/``<tbody>``, ``scope`` attributes and a
  ``<caption>``, so a screen reader can announce cells by column;
* numbers right-aligned in a tabular-figures font and never wrapped, labels
  left-aligned and allowed to wrap, which is what makes a column of figures
  readable at all;
* a scroll container kept as a second line of defence for readers that do
  support panning.
"""

import re
from dataclasses import replace
from typing import List, Optional, Sequence

from .table_detector import Cell, DetectedTable, TableDetector

# A table's width is estimated in "character units": the widest value in each
# column, plus two per column for padding. Measured against a 600pt-wide e-ink
# screen at 0.72em, tables estimating 104-106 units fit exactly and 113 clips,
# so the budget is set just below that boundary.
DEFAULT_WIDTH_BUDGET = 105

# A hard cap regardless of how narrow the values are: past this many columns a
# row is hard to follow across even when it technically fits.
DEFAULT_MAX_COLUMNS = 12

# Emitted once per chapter that contains a table. The frozen EPUB stylesheet
# has no table rules, so the styling travels with the content.
TABLE_STYLESHEET = """<style type="text/css">
.table-wrap { overflow-x: auto; margin: 1.2em 0; }
table.exhibit {
  border-collapse: collapse;
  font-size: 0.72em;
  line-height: 1.25;
  width: 100%;
  table-layout: auto;
}
table.exhibit caption {
  caption-side: top;
  font-size: 1.05em;
  font-weight: bold;
  text-align: left;
  padding-bottom: 0.4em;
}
table.exhibit th, table.exhibit td {
  padding: 0.18em 0.4em;
  vertical-align: bottom;
}
table.exhibit thead th {
  border-bottom: 1px solid currentColor;
  font-weight: bold;
  text-align: center;
  /* Header words may wrap; that costs a line, not the whole column. */
  white-space: normal;
}
table.exhibit td.num, table.exhibit th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  /* A number broken across lines is unreadable. */
  white-space: nowrap;
}
table.exhibit td.label, table.exhibit th.label { text-align: left; }
table.exhibit tbody tr.total td { border-top: 1px solid currentColor; font-weight: bold; }
.table-part-note {
  font-size: 0.68em;
  font-style: italic;
  margin: 0 0 0.2em 0;
}
</style>"""


class TableRenderer:
    """Converts a DetectedTable into XHTML."""

    def __init__(
        self,
        width_budget: int = DEFAULT_WIDTH_BUDGET,
        max_columns: int = DEFAULT_MAX_COLUMNS,
    ):
        """
        Args:
            width_budget: Estimated width, in character units, that one part
                may occupy. Tables wider than this are split.
            max_columns: Hard cap on columns per part, whatever the widths.
        """
        self.width_budget = max(8, width_budget)
        self.max_columns = max(2, max_columns)

    def render(self, table: DetectedTable, caption: Optional[str] = None) -> str:
        """
        Render one table, splitting it across column groups if it is too wide.

        Args:
            table: The reconstructed table.
            caption: Caption text; falls back to the table's own caption.

        Returns:
            An XHTML fragment.
        """
        title = caption or table.caption
        widths = self._column_widths(table)

        if (sum(widths) <= self.width_budget
                and table.column_count <= self.max_columns):
            return self._render_part(table, title, indices=None, part=None, parts_total=1)

        label_count = self._label_column_count(table)
        groups = self._column_groups(widths, label_count, table.column_count)

        if len(groups) <= 1:
            return self._render_part(table, title, indices=None, part=None, parts_total=1)

        rendered = []
        for number, group in enumerate(groups, start=1):
            indices = list(range(label_count)) + group
            rendered.append(self._render_part(
                table, title, indices=indices, part=number, parts_total=len(groups),
                first_data_column=group[0] + 1, last_data_column=group[-1] + 1,
            ))
        return "\n".join(rendered)

    def _column_widths(self, table: DetectedTable) -> List[int]:
        """
        Estimated width of each column in character units.

        The widest value decides, plus two units of padding. Cells that span
        several columns are ignored: they wrap, so they do not force any one
        column wider. Header text is measured by its longest *word*, since
        headers are allowed to wrap too.
        """
        count = table.column_count
        widths = [0] * count
        for row in table.rows:
            for index, cell in enumerate(self._expand(row, count)):
                if cell is None or cell.colspan > 1:
                    continue
                text = cell.text.strip()
                if not text:
                    continue
                longest = (max((len(word) for word in text.split()), default=0)
                           if cell.is_header else len(text))
                widths[index] = max(widths[index], longest)
        return [w + 2 for w in widths]

    def _column_groups(
        self,
        widths: List[int],
        label_count: int,
        column_count: int
    ) -> List[List[int]]:
        """
        Divide the data columns into groups that each fit the budget.

        The label columns ride along in every group, so their width is charged
        against every group's budget.
        """
        label_width = sum(widths[:label_count])
        budget = max(self.width_budget - label_width, min(widths[label_count:], default=1))
        per_part_cap = max(1, self.max_columns - label_count)

        groups: List[List[int]] = []
        current: List[int] = []
        used = 0

        for index in range(label_count, column_count):
            width = widths[index]
            if current and (used + width > budget or len(current) >= per_part_cap):
                groups.append(current)
                current, used = [], 0
            current.append(index)
            used += width

        if current:
            groups.append(current)
        return groups

    def _render_part(
        self,
        table: DetectedTable,
        title: Optional[str],
        indices: Optional[List[int]],
        part: Optional[int],
        parts_total: int,
        first_data_column: int = 0,
        last_data_column: int = 0,
    ) -> str:
        parts: List[str] = ['<div class="table-wrap">']

        if part is not None:
            parts.append(
                f'<p class="table-part-note">Part {part} of {parts_total} — '
                f'columns {first_data_column}–{last_data_column} of '
                f'{table.column_count}, row labels repeated</p>'
            )

        parts.append('<table class="exhibit">')

        if title:
            parts.append(f"  <caption>{self._escape(title)}</caption>")

        header_rows = table.rows[:table.header_row_count]
        body_rows = table.rows[table.header_row_count:]

        if header_rows:
            parts.append("  <thead>")
            for row in header_rows:
                cells = self._select(row, table.column_count, indices)
                parts.append("    <tr>" + "".join(self._header_cell(c) for c in cells) + "</tr>")
            parts.append("  </thead>")

        parts.append("  <tbody>")
        for row in body_rows:
            cells = self._select(row, table.column_count, indices)
            css_class = ' class="total"' if self._is_total_row(cells) else ""
            parts.append(f"    <tr{css_class}>" + "".join(self._body_cell(c) for c in cells) + "</tr>")
        parts.append("  </tbody>")

        parts.append("</table>")
        parts.append("</div>")
        return "\n".join(parts)

    # ------------------------------------------------------- column slicing

    @staticmethod
    def _expand(row: Sequence[Cell], column_count: int) -> List[Optional[Cell]]:
        """One entry per column, repeating a cell across the columns it spans."""
        expanded: List[Optional[Cell]] = []
        for cell in row:
            expanded.extend([cell] * max(1, cell.colspan))
        expanded = expanded[:column_count]
        expanded.extend([None] * (column_count - len(expanded)))
        return expanded

    def _select(
        self,
        row: Sequence[Cell],
        column_count: int,
        indices: Optional[List[int]]
    ) -> List[Cell]:
        """
        Take the chosen columns from a row, re-forming colspans.

        A header spanning columns 3-7 keeps a colspan of 3 when only columns
        3-5 are in this group, and disappears entirely when none are.
        """
        if indices is None:
            return list(row)

        expanded = self._expand(row, column_count)
        picked = [expanded[i] for i in indices]

        cells: List[Cell] = []
        current: Optional[Cell] = None
        span = 0
        for cell in picked:
            # Identity, not equality: two separate cells holding the same text
            # are still two cells.
            if cell is not None and cell is current:
                span += 1
                continue
            if span:
                cells.append(replace(current, colspan=span) if current is not None
                             else Cell(text="", colspan=span))
            current, span = cell, 1
        if span:
            cells.append(replace(current, colspan=span) if current is not None
                         else Cell(text="", colspan=span))
        return cells

    @staticmethod
    def _label_column_count(table: DetectedTable) -> int:
        """
        How many leading columns identify the row rather than carry data.

        These are repeated in every column group, so a reader looking at
        columns 9-11 still knows which accident year each row belongs to —
        which is the whole point of splitting rather than clipping.

        An exhibit often opens with an empty spacer column before the accident
        year, so an empty leading column is carried along rather than ending
        the search, and a column of years counts as naming the row even though
        it reads as numeric.
        """
        body = table.rows[table.header_row_count:]
        if not body:
            return 1

        count = 0
        limit = min(2, max(1, table.column_count - 1))
        for index in range(limit):
            values = []
            for row in body:
                cell = TableRenderer._expand(row, table.column_count)[index]
                if cell is not None and cell.text.strip():
                    values.append(cell.text.strip())

            if not values:
                # An empty spacer column: keep it and look at the next one.
                count += 1
                continue

            years = sum(1 for v in values if re.fullmatch(r"(19|20)\d{2}", v))
            non_numeric = sum(1 for v in values if not TableDetector.is_numeric(v))
            if index == 0 or years >= len(values) * 0.6 or non_numeric >= len(values) * 0.6:
                count += 1
            else:
                break

        # Always leave at least one data column to show.
        return max(1, min(count, table.column_count - 1))

    def render_figure(
        self,
        filename: str,
        anchor_id: str,
        alt_text: str,
        caption: Optional[str] = None
    ) -> str:
        """Render a chart image as a figure."""
        caption_html = f"\n  <figcaption>{self._escape(caption)}</figcaption>" if caption else ""
        return (
            f'<figure class="chart" id="{anchor_id}">\n'
            f'  <img src="images/{filename}" alt="{self._escape(alt_text)}"/>'
            f'{caption_html}\n'
            f'</figure>'
        )

    # ------------------------------------------------------------- internals

    def _header_cell(self, cell: Cell) -> str:
        span = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ""
        scope = ' scope="colgroup"' if cell.colspan > 1 else ' scope="col"'
        css = "num" if cell.numeric else "label"
        return f'<th{span}{scope} class="{css}">{self._escape(cell.text)}</th>'

    def _body_cell(self, cell: Cell) -> str:
        span = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ""
        css = "num" if cell.numeric else "label"
        # The leading cell of a data row names the row: mark it up as a header.
        return f'<td{span} class="{css}">{self._escape(cell.text)}</td>'

    @staticmethod
    def _is_total_row(row: List[Cell]) -> bool:
        for cell in row:
            text = cell.text.strip().lower()
            if text:
                return text.startswith("total")
        return False

    @staticmethod
    def _escape(text: str) -> str:
        if not text:
            return ""
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))

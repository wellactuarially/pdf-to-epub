# ADAPTABLE: Table markup and styling can be tuned
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Renders reconstructed tables as EPUB-friendly XHTML.

Wide numeric exhibits are the hard case for e-readers: a 12-column claim
triangle cannot fit a phone screen at a readable size. The markup here is
built so that it degrades sensibly instead of breaking:

* a real ``<table>`` with ``<thead>``/``<tbody>``, ``scope`` attributes and a
  ``<caption>``, so a screen reader can announce cells by column;
* the table wrapped in a scrolling container, so a reader that cannot shrink
  it lets the user pan instead of clipping the right-hand columns;
* numbers right-aligned in a tabular-figures font and labels left-aligned,
  which is what makes a column of figures readable at all.
"""

from typing import List, Optional

from .table_detector import Cell, DetectedTable

# Emitted once per chapter that contains a table. The frozen EPUB stylesheet
# has no table rules, so the styling travels with the content.
TABLE_STYLESHEET = """<style type="text/css">
.table-wrap { overflow-x: auto; margin: 1.2em 0; }
table.exhibit {
  border-collapse: collapse;
  font-size: 0.72em;
  line-height: 1.25;
  width: 100%;
}
table.exhibit caption {
  caption-side: top;
  font-size: 1.05em;
  font-weight: bold;
  text-align: left;
  padding-bottom: 0.4em;
}
table.exhibit th, table.exhibit td {
  padding: 0.18em 0.45em;
  vertical-align: bottom;
  white-space: nowrap;
}
table.exhibit thead th {
  border-bottom: 1px solid currentColor;
  font-weight: bold;
  text-align: center;
}
table.exhibit td.num, table.exhibit th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
table.exhibit td.label, table.exhibit th.label { text-align: left; }
table.exhibit tbody tr.total td { border-top: 1px solid currentColor; font-weight: bold; }
</style>"""


class TableRenderer:
    """Converts a DetectedTable into XHTML."""

    def render(self, table: DetectedTable, caption: Optional[str] = None) -> str:
        """
        Render one table.

        Args:
            table: The reconstructed table.
            caption: Caption text; falls back to the table's own caption.

        Returns:
            An XHTML fragment.
        """
        # Wide exhibits are set smaller so they have a chance of fitting a
        # narrow screen: readers that ignore the scroll container would
        # otherwise clip the right-hand columns outright.
        columns = max(1, table.column_count)
        font_size = max(0.55, 0.78 - 0.02 * max(0, columns - 6))
        parts: List[str] = [
            '<div class="table-wrap">',
            f'<table class="exhibit" style="font-size: {font_size:.2f}em;">',
        ]

        title = caption or table.caption
        if title:
            parts.append(f"  <caption>{self._escape(title)}</caption>")

        header_rows = table.rows[:table.header_row_count]
        body_rows = table.rows[table.header_row_count:]

        if header_rows:
            parts.append("  <thead>")
            for row in header_rows:
                parts.append("    <tr>" + "".join(self._header_cell(c) for c in row) + "</tr>")
            parts.append("  </thead>")

        parts.append("  <tbody>")
        for row in body_rows:
            css_class = ' class="total"' if self._is_total_row(row) else ""
            parts.append(f"    <tr{css_class}>" + "".join(self._body_cell(c) for c in row) + "</tr>")
        parts.append("  </tbody>")

        parts.append("</table>")
        parts.append("</div>")
        return "\n".join(parts)

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

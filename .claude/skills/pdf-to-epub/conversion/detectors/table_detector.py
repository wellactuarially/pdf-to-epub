# ADAPTABLE: Table detection heuristics can be tuned
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Reconstructs tabular regions of a PDF page into a real cell grid.

Aimed at the kind of tables that dominate technical and actuarial books:
Excel-generated exhibits with right-aligned numeric columns, multi-level
spanning headers, and no (or only partial) ruling lines.

Algorithm
---------
1. Words are read in the page's *visual* space (rotation applied) and grouped
   into lines by vertical overlap.
2. Lines are split into bands separated by vertical whitespace, so several
   stacked tables on one page are detected independently.
3. Within a band, column anchors are derived from the right edges of tokens on
   *numeric data rows only*. Spanning header text ("Trend Adjustment" over five
   year columns) therefore cannot smear the column boundaries.
4. Every token is then assigned to the column span it overlaps, which yields
   colspan values for header rows for free.
5. A band is accepted as a table only if it looks like one: enough rows, enough
   columns, and enough numeric cells.
"""

import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import fitz

from core.utils import get_logger

logger = get_logger(__name__)

# A token counts as numeric if digits dominate. Currency, percentages, thousands
# separators, parenthesised negatives and en-dash placeholders all qualify.
_NUMERIC_CLEAN = re.compile(r"[\s,.$%()\[\]–—+\-]")


@dataclass
class Cell:
    """One cell of a reconstructed table."""
    text: str
    colspan: int = 1
    is_header: bool = False
    numeric: bool = False


@dataclass
class DetectedTable:
    """A reconstructed table together with where it came from."""
    page: int
    bbox: Tuple[float, float, float, float]
    rows: List[List[Cell]] = field(default_factory=list)
    header_row_count: int = 0
    caption: Optional[str] = None

    @property
    def column_count(self) -> int:
        return max((sum(c.colspan for c in row) for row in self.rows), default=0)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def cell_texts(self) -> List[str]:
        return [c.text for row in self.rows for c in row if c.text.strip()]


class TableDetector:
    """Finds and reconstructs tables on a PDF page."""

    def __init__(
        self,
        min_rows: int = 3,
        min_columns: int = 2,
        min_numeric_ratio: float = 0.35,
        column_gap: float = 4.0,
        band_gap_factor: float = 1.9,
    ):
        """
        Args:
            min_rows: Minimum number of rows for a band to count as a table.
            min_columns: Minimum number of columns for a band to count as a table.
            min_numeric_ratio: Minimum share of non-empty cells that must look
                numeric. Lower it for word tables, raise it to only catch
                number grids.
            column_gap: Horizontal gap (pt) that separates two cells on a row.
            band_gap_factor: Vertical gap, in multiples of the median line
                pitch, that starts a new band.
        """
        self.min_rows = min_rows
        self.min_columns = min_columns
        self.min_numeric_ratio = min_numeric_ratio
        self.column_gap = column_gap
        self.band_gap_factor = band_gap_factor

    # ---------------------------------------------------------------- public

    def detect(self, page: "fitz.Page", exclude: Sequence[fitz.Rect] = ()) -> List[DetectedTable]:
        """
        Detect all tables on a page.

        Args:
            page: PyMuPDF page.
            exclude: Regions to ignore (e.g. chart areas already claimed).

        Returns:
            Tables in top-to-bottom order.
        """
        words = self._visual_words(page)
        if exclude:
            words = [w for w in words if not any(self._mostly_inside(w, r) for r in exclude)]
        if not words:
            return []

        tables: List[DetectedTable] = []
        for band in self._split_bands(self._group_lines(words)):
            table = self._build_table(band, page.number + 1)
            if table is not None:
                tables.append(table)
        return tables

    # ------------------------------------------------------------- internals

    @staticmethod
    def _visual_words(page: "fitz.Page") -> List[Tuple[float, float, float, float, str]]:
        """Words with coordinates mapped into the page's visual (rotated) space."""
        matrix = page.rotation_matrix
        out = []
        for w in page.get_text("words"):
            if not w[4].strip():
                continue
            rect = fitz.Rect(w[:4]) * matrix
            out.append((rect.x0, rect.y0, rect.x1, rect.y1, w[4]))
        return out

    @staticmethod
    def _mostly_inside(word, rect: fitz.Rect) -> bool:
        word_rect = fitz.Rect(word[0], word[1], word[2], word[3])
        overlap = word_rect & rect
        if overlap.is_empty:
            return False
        area = word_rect.get_area()
        return area > 0 and overlap.get_area() / area > 0.5

    def _group_lines(self, words) -> List[List]:
        """Group words into lines by vertical overlap."""
        heights = [w[3] - w[1] for w in words]
        tolerance = statistics.median(heights) * 0.6 if heights else 4.0

        words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        lines, current = [], [words[0]]
        midpoint = (words[0][1] + words[0][3]) / 2

        for word in words[1:]:
            word_mid = (word[1] + word[3]) / 2
            if abs(word_mid - midpoint) <= tolerance:
                current.append(word)
                midpoint = sum((w[1] + w[3]) / 2 for w in current) / len(current)
            else:
                lines.append(sorted(current, key=lambda w: w[0]))
                current, midpoint = [word], word_mid

        lines.append(sorted(current, key=lambda w: w[0]))
        return lines

    def _split_bands(self, lines: List[List]) -> List[List[List]]:
        """Split lines into blocks separated by vertical whitespace."""
        if not lines:
            return []

        tops = [min(w[1] for w in line) for line in lines]
        bottoms = [max(w[3] for w in line) for line in lines]
        pitches = [tops[i + 1] - tops[i] for i in range(len(lines) - 1)]
        pitch = statistics.median(pitches) if pitches else 12.0

        bands, current = [], [lines[0]]
        for i in range(1, len(lines)):
            if tops[i] - bottoms[i - 1] > pitch * self.band_gap_factor:
                bands.append(current)
                current = [lines[i]]
            else:
                current.append(lines[i])
        bands.append(current)
        return bands

    def _tokenize(self, line: List) -> List[Tuple[float, float, str]]:
        """Merge adjacent words on a line into cell-sized tokens."""
        tokens = []
        current = [line[0]]
        for word in line[1:]:
            if word[0] - current[-1][2] >= self.column_gap:
                tokens.append(current)
                current = [word]
            else:
                current.append(word)
        tokens.append(current)

        return [
            (t[0][0], max(w[2] for w in t), " ".join(w[4] for w in t).strip())
            for t in tokens
        ]

    @staticmethod
    def is_numeric(text: str) -> bool:
        """True when a cell reads as a number rather than a label."""
        stripped = text.strip()
        if not stripped:
            return False
        if stripped in {"-", "–", "—", "N/A", "n/a"}:
            return True
        residue = _NUMERIC_CLEAN.sub("", stripped)
        if not residue:
            return False
        digits = sum(c.isdigit() for c in residue)
        return digits > 0 and digits >= len(residue) * 0.8

    def _data_rows(self, rows: List[List[Tuple[float, float, str]]]) -> List[List[Tuple[float, float, str]]]:
        """Rows that carry numbers rather than labels."""
        return [
            row for row in rows
            if len(row) >= self.min_columns
            and sum(self.is_numeric(t[2]) for t in row) >= len(row) * 0.6
        ]

    @staticmethod
    def _looks_like_label_values(values: List[str]) -> bool:
        """
        Numeric-looking values that are really headings: the "(1) (2) (3)"
        column keys, and the year captions sitting under a spanning group
        header.
        """
        values = [value for value in values if value.strip()]
        if not values:
            return False
        if all(re.fullmatch(r"\(\d{1,3}\)", value.strip()) for value in values):
            return True
        years = [v for v in values if re.fullmatch(r"(19|20)\d{2}", v.strip())]
        return len(years) >= max(2, len(values) - 2)

    @classmethod
    def _is_label_row(cls, row: List[Tuple[float, float, str]]) -> bool:
        """
        A heading row masquerading as data.

        These are centred over their columns rather than aligned to them, so
        letting them vote on column positions would split every column in two.
        """
        return cls._looks_like_label_values([token[2] for token in row])

    def _column_anchors(self, rows: List[List[Tuple[float, float, str]]]) -> List[float]:
        """
        Derive column positions from the right edges of numeric data rows.

        Numeric columns in generated exhibits are right-aligned, so the right
        edge of a given column is stable down the table. Edges are clustered
        and each cluster must be backed by several rows, which keeps blank
        cells and ragged rows from inventing columns.
        """
        data_rows = [row for row in self._data_rows(rows) if not self._is_label_row(row)]
        if len(data_rows) < 2:
            return []

        edges = sorted(token[1] for row in data_rows for token in row)
        clusters, current = [], [edges[0]]
        for edge in edges[1:]:
            if edge - current[-1] <= 6.0:
                current.append(edge)
            else:
                clusters.append(current)
                current = [edge]
        clusters.append(current)

        support = max(2, len(data_rows) * 0.3)
        anchors = sorted(statistics.median(c) for c in clusters if len(c) >= support)

        # Two anchors closer together than a narrow cell are one column.
        merged: List[float] = []
        for anchor in anchors:
            if merged and anchor - merged[-1] < 9.0:
                merged[-1] = (merged[-1] + anchor) / 2
            else:
                merged.append(anchor)
        return merged

    def _recover_sparse_columns(
        self,
        anchors: List[float],
        rows: List[List[Tuple[float, float, str]]]
    ) -> List[float]:
        """
        Add columns for values that fit nowhere.

        A column filled on only a few rows — a transaction year that most
        claims have no entry for — never gathers enough support to survive
        clustering. Rather than fold those values into a neighbour, give them
        their own column. Only numeric tokens vote: header text legitimately
        spans several columns.
        """
        for _ in range(3):
            uncovered = sorted(
                token[1] for row in rows for token in row
                if self.is_numeric(token[2])
                and all(abs(token[1] - anchor) > 9.0 for anchor in anchors)
            )
            if not uncovered:
                break

            clusters, current = [], [uncovered[0]]
            for edge in uncovered[1:]:
                if edge - current[-1] <= 6.0:
                    current.append(edge)
                else:
                    clusters.append(current)
                    current = [edge]
            clusters.append(current)

            additions = [statistics.median(c) for c in clusters if len(c) >= 2]
            if not additions:
                break
            anchors = sorted(anchors + additions)

        return anchors

    def _column_bounds(
        self,
        anchors: List[float],
        rows: List[List[Tuple[float, float, str]]]
    ) -> List[Tuple[float, float]]:
        """
        Turn right-edge anchors into [left, right] column intervals, adding
        extra leading columns for row labels that align to nothing numeric.
        """
        tolerance = 8.0
        bounds: List[Tuple[float, float]] = []
        for index, anchor in enumerate(anchors):
            lefts = [
                token[0] for row in rows for token in row
                if abs(token[1] - anchor) <= tolerance
            ]
            left = min(lefts) if lefts else anchor - 40.0
            if index > 0:
                # Never reach back past the previous column's right edge.
                left = max(left, anchors[index - 1] + 1.0)
            bounds.append((left, anchor + tolerance))

        # Tokens sitting entirely left of the first column are row labels.
        first_left = bounds[0][0] if bounds else 0.0
        orphans = [
            token for row in rows for token in row
            if token[1] < first_left - 1.0
        ]
        if orphans:
            bounds.insert(0, (min(t[0] for t in orphans) - 2.0, first_left - 1.0))

        return bounds

    @staticmethod
    def _is_prose_row(row: List[Tuple[float, float, str]]) -> bool:
        """
        Running text rather than table cells.

        Justified prose breaks into a couple of wide tokens when word spacing
        stretches; real cells stay short. Word count per token separates them.
        """
        if len(row) < 2:
            return True
        return max(len(token[2].split()) for token in row) > 6

    def _table_row_span(
        self,
        rows: List[List[Tuple[float, float, str]]]
    ) -> Optional[Tuple[int, int]]:
        """
        Narrow a band down to the rows that actually form the table.

        Seeded from the run of numeric rows, then grown outwards over header
        lines, stopping as soon as a line reads like prose.
        """
        numeric_indices = [
            index for index, row in enumerate(rows)
            if row in self._data_rows(rows)
        ]
        if len(numeric_indices) < 2:
            return None

        start, end = min(numeric_indices), max(numeric_indices)
        while start > 0 and not self._is_prose_row(rows[start - 1]):
            start -= 1
        while end < len(rows) - 1 and not self._is_prose_row(rows[end + 1]):
            end += 1
        return start, end

    def _build_table(self, band: List[List], page_number: int) -> Optional[DetectedTable]:
        all_rows = [self._tokenize(line) for line in band]
        span = self._table_row_span(all_rows)
        if span is None:
            return None

        start, end = span
        rows = all_rows[start:end + 1]
        band = band[start:end + 1]

        anchors = self._column_anchors(rows)
        if len(anchors) < self.min_columns:
            return None

        anchors = self._recover_sparse_columns(anchors, rows)
        bounds = self._column_bounds(anchors, rows)
        column_count = len(bounds)
        if column_count < self.min_columns:
            return None

        grid: List[List[Cell]] = []
        for row in rows:
            cells = self._place_row(row, bounds)
            if cells:
                grid.append(cells)

        if len(grid) < self.min_rows:
            return None

        non_empty = [c for row in grid for c in row if c.text.strip()]
        if not non_empty:
            return None
        numeric_ratio = sum(1 for c in non_empty if c.numeric) / len(non_empty)
        if numeric_ratio < self.min_numeric_ratio:
            return None

        header_rows = self._count_header_rows(grid)
        for row in grid[:header_rows]:
            for cell in row:
                cell.is_header = True

        x0 = min(w[0] for line in band for w in line)
        y0 = min(w[1] for line in band for w in line)
        x1 = max(w[2] for line in band for w in line)
        y1 = max(w[3] for line in band for w in line)

        return DetectedTable(
            page=page_number,
            bbox=(x0, y0, x1, y1),
            rows=grid,
            header_row_count=header_rows,
        )

    def _place_row(
        self,
        row: List[Tuple[float, float, str]],
        bounds: List[Tuple[float, float]]
    ) -> List[Cell]:
        """Assign a row's tokens to columns, producing colspans for wide tokens."""
        column_count = len(bounds)
        slots: List[Optional[Cell]] = [None] * column_count
        occupied = [False] * column_count

        for left, right, text in row:
            start, end = None, None
            for index, (col_left, col_right) in enumerate(bounds):
                # A token belongs to every column whose interval it overlaps.
                if right >= col_left and left <= col_right:
                    if start is None:
                        start = index
                    end = index
            if start is None:
                # Right of the last column (rare): fold into the final column.
                start = end = column_count - 1

            # Right-aligned numbers must not bleed left into the previous column.
            if self.is_numeric(text) and end is not None and end > start:
                start = end

            while start < column_count and occupied[start] and start < end:
                start += 1
            span = max(1, (end - start + 1)) if end is not None else 1
            span = min(span, column_count - start)

            if slots[start] is not None:
                slots[start].text = (slots[start].text + " " + text).strip()
                continue

            slots[start] = Cell(text=text, colspan=span, numeric=self.is_numeric(text))
            for offset in range(span):
                occupied[start + offset] = True

        cells: List[Cell] = []
        index = 0
        while index < column_count:
            cell = slots[index]
            if cell is None:
                cells.append(Cell(text="", colspan=1))
                index += 1
            else:
                cells.append(cell)
                index += cell.colspan
        return cells

    @classmethod
    def _count_header_rows(cls, grid: List[List[Cell]]) -> int:
        """
        How many leading rows form the header.

        A row of nothing but numbers is not automatically data: an exhibit's
        header is routinely three rows deep — a spanning group title, a row of
        year captions, then the "(1) (2) (3)" column keys. Treating the year
        row as data leaves the columns unlabelled, which matters most when a
        wide table is split and each part has to carry its own header.
        """
        header_rows = 0
        for row in grid:
            values = [c for c in row if c.text.strip()]
            if not values:
                header_rows += 1
                continue
            numeric = sum(1 for c in values if c.numeric)
            if numeric == 0 or cls._looks_like_label_values([c.text for c in values]):
                header_rows += 1
            else:
                break
        return min(header_rows, max(0, len(grid) - 1))

"""Tests for table reconstruction, rendering, and the exhibit strategy.

Run from the skill directory:  python -m pytest tests/ -v
"""

import fitz
import pytest

from conversion.detectors.table_detector import Cell, DetectedTable, TableDetector
from conversion.detectors.table_renderer import TableRenderer


def make_page(lines, landscape=False):
    """
    Build a one-page PDF from (x, y, text) triples and return its page.

    Text is placed at absolute positions so the detector sees the same kind of
    geometry it gets from a real exhibit.
    """
    doc = fitz.open()
    width, height = (792, 612) if landscape else (612, 792)
    page = doc.new_page(width=width, height=height)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=9, fontname="helv")
    return doc, doc[0]


def make_rotated_page(lines):
    """
    Build the landscape exhibit page as real PDFs do: a portrait page carrying
    /Rotate 90, with the text drawn sideways so it reads upright once rotated.

    The (x, y) in `lines` are the coordinates the reader sees.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for x, y, text in lines:
        # Visual (x, y) maps back to unrotated (y, page_height - x).
        page.insert_text((y, 792 - x), text, fontsize=9, fontname="helv", rotate=270)
    page.set_rotation(90)
    return doc, doc[0]


def numeric_grid(x_positions, y_start=100, rows=6):
    """A header row plus right-aligned numeric rows."""
    lines = []
    for index, x in enumerate(x_positions):
        lines.append((x, y_start, f"Col{index}"))
    for row in range(rows):
        y = y_start + 20 + row * 14
        lines.append((x_positions[0], y, f"200{row}"))
        for x in x_positions[1:]:
            lines.append((x, y, f"{row + 1}.{row}00"))
    return lines


class TestIsNumeric:
    @pytest.mark.parametrize("text", [
        "1.070", "48,953", "79.5%", "2002", "(12)", "-", "N/A", "$1,234.56", "1,500,000",
    ])
    def test_numeric_values(self, text):
        assert TableDetector.is_numeric(text)

    @pytest.mark.parametrize("text", [
        "Accident", "Total", "e.g.", "Year", "Jan-5-05", "", "   ",
    ])
    def test_non_numeric_values(self, text):
        assert not TableDetector.is_numeric(text)


class TestTableDetection:
    def test_detects_simple_numeric_grid(self):
        doc, page = make_page(numeric_grid([80, 200, 300, 400]))
        try:
            tables = TableDetector().detect(page)
            assert len(tables) == 1
            assert tables[0].column_count == 4
            assert tables[0].row_count >= 6
        finally:
            doc.close()

    def test_ignores_prose(self):
        prose = [
            (80, 100 + i * 14, "The actuary reviews the claim development triangle carefully")
            for i in range(8)
        ]
        doc, page = make_page(prose)
        try:
            assert TableDetector().detect(page) == []
        finally:
            doc.close()

    def test_prose_above_table_is_not_absorbed(self):
        lines = [(80, 60 + i * 14, "Each column represents an age and is directly related")
                 for i in range(4)]
        lines += numeric_grid([80, 200, 300, 400], y_start=200)
        doc, page = make_page(lines)
        try:
            tables = TableDetector().detect(page)
            assert len(tables) == 1
            flat = " ".join(tables[0].cell_texts())
            assert "actuary" not in flat and "represents" not in flat
        finally:
            doc.close()

    def test_two_stacked_tables_detected_separately(self):
        lines = numeric_grid([80, 200, 300, 400], y_start=100, rows=5)
        lines += numeric_grid([80, 200, 300, 400], y_start=300, rows=5)
        doc, page = make_page(lines)
        try:
            assert len(TableDetector().detect(page)) == 2
        finally:
            doc.close()

    def test_rotated_page_reconstructs_like_an_upright_one(self):
        """
        Landscape exhibits are portrait pages with /Rotate 90. Text coordinates
        come back unrotated, so without applying the rotation matrix the rows
        of such a table are read as columns and the grid is scrambled.
        """
        lines = numeric_grid([80, 200, 300, 400])
        upright_doc, upright = make_page(lines)
        rotated_doc, rotated = make_rotated_page(lines)
        try:
            a = TableDetector().detect(upright)
            b = TableDetector().detect(rotated)
            assert len(a) == len(b) == 1
            assert a[0].column_count == b[0].column_count
            assert a[0].cell_texts() == b[0].cell_texts()
        finally:
            upright_doc.close()
            rotated_doc.close()

    def test_rotated_words_are_mapped_to_visual_space(self):
        doc, page = make_rotated_page(numeric_grid([80, 200, 300, 400]))
        try:
            words = TableDetector._visual_words(page)
            # Upright text is wider than it is tall; sideways text is not.
            assert all(w[2] - w[0] > w[3] - w[1] for w in words)
        finally:
            doc.close()

    def test_respects_excluded_regions(self):
        doc, page = make_page(numeric_grid([80, 200, 300, 400]))
        try:
            everything = fitz.Rect(0, 0, 612, 792)
            assert TableDetector().detect(page, exclude=[everything]) == []
        finally:
            doc.close()

    def test_sparse_column_gets_its_own_column(self):
        """Values present on only a few rows must not fold into a neighbour."""
        lines = numeric_grid([80, 200, 300, 400], rows=6)
        lines.append((500, 134, "9,999"))
        lines.append((500, 148, "8,888"))
        doc, page = make_page(lines)
        try:
            tables = TableDetector().detect(page)
            assert tables[0].column_count == 5
            assert "9,999" in tables[0].cell_texts()
            assert "8,888" in tables[0].cell_texts()
        finally:
            doc.close()

    def test_min_columns_is_enforced(self):
        lines = numeric_grid([80, 200], rows=6)
        doc, page = make_page(lines)
        try:
            assert TableDetector(min_columns=4).detect(page) == []
        finally:
            doc.close()


class TestTableRenderer:
    def build(self):
        return DetectedTable(
            page=1,
            bbox=(0, 0, 100, 100),
            rows=[
                [Cell("Year", is_header=True), Cell("Claims", colspan=2, is_header=True)],
                [Cell("2002"), Cell("48,953", numeric=True), Cell("1.070", numeric=True)],
                [Cell("Total"), Cell("97,906", numeric=True), Cell("2.140", numeric=True)],
            ],
            header_row_count=1,
        )

    def test_renders_semantic_table(self):
        html = TableRenderer().render(self.build())
        assert "<thead>" in html and "<tbody>" in html
        assert 'colspan="2"' in html
        assert 'scope="colgroup"' in html
        assert 'class="table-wrap"' in html  # scroll container for wide tables

    def test_numeric_cells_are_right_aligned(self):
        html = TableRenderer().render(self.build())
        assert '<td class="num">48,953</td>' in html
        assert '<td class="label">2002</td>' in html

    def test_total_row_marked(self):
        assert 'class="total"' in TableRenderer().render(self.build())

    def test_caption_and_escaping(self):
        html = TableRenderer().render(self.build(), caption='Exhibit <I> & "II"')
        assert "<caption>" in html
        assert "&lt;I&gt;" in html and "&amp;" in html and "&quot;" in html

    def test_figure_markup(self):
        html = TableRenderer().render_figure("chart001.png", "chart-1", "Severity trend")
        assert 'src="images/chart001.png"' in html
        assert 'alt="Severity trend"' in html

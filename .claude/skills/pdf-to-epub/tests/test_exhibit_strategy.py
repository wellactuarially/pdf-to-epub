"""Tests for chart extraction, the exhibit strategy, and rotated-page handling.

Run from the skill directory:  python -m pytest tests/ -v
"""

import zipfile

import fitz
import pytest

from conversion.converter import Converter
from conversion.detectors.chart_detector import ChartDetector
from conversion.strategies.exhibit_strategy import ExhibitStrategy, PlaceholderBlock
from core.pdf_extractor import PDFExtractor


def draw_chart(page, rect):
    """Draw something plot-like: a frame, gridlines, and a data line."""
    shape = page.new_shape()
    shape.draw_rect(rect)
    steps = 8
    for i in range(1, steps):
        y = rect.y0 + rect.height * i / steps
        shape.draw_line(fitz.Point(rect.x0, y), fitz.Point(rect.x1, y))
    points = [
        fitz.Point(rect.x0 + rect.width * i / 10,
                   rect.y1 - rect.height * (i ** 1.6) / 40)
        for i in range(11)
    ]
    for a, b in zip(points, points[1:]):
        shape.draw_line(a, b)
    shape.finish(width=0.6)
    shape.commit()
    # A few axis labels, as a real chart has.
    for i in range(5):
        page.insert_text((rect.x0 - 24, rect.y1 - rect.height * i / 4),
                         f"{i * 25}%", fontsize=8, fontname="helv")


@pytest.fixture
def chart_page():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((80, 60), "Sensitivity of Unpaid Claim Estimate", fontsize=12, fontname="hebo")
    draw_chart(page, fitz.Rect(120, 120, 520, 520))
    yield doc, doc[0]
    doc.close()


class TestChartDetector:
    def test_detects_and_rasterizes_a_chart(self, chart_page):
        _, page = chart_page
        charts = ChartDetector().detect(page)
        assert len(charts) == 1
        chart = charts[0]
        assert chart.data[:8] == b"\x89PNG\r\n\x1a\n"
        assert chart.width > 0 and chart.height > 0
        assert chart.page == 1

    def test_dense_table_region_is_not_a_chart(self):
        """A ruled table is drawings plus a lot of text: not a plot."""
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        shape = page.new_shape()
        for i in range(12):
            y = 120 + i * 24
            shape.draw_line(fitz.Point(100, y), fitz.Point(500, y))
        shape.finish(width=0.5)
        shape.commit()
        for row in range(11):
            for col in range(6):
                page.insert_text((110 + col * 65, 136 + row * 24),
                                 f"{row},{col}00", fontsize=8, fontname="helv")
        try:
            assert ChartDetector().detect(page) == []
        finally:
            doc.close()

    def test_caption_uses_labels_inside_the_plot(self, chart_page):
        _, page = chart_page
        chart = ChartDetector().detect(page)[0]
        caption = ExhibitStrategy._chart_caption(chart)
        assert caption
        assert len(caption) <= 160


class TestRotatedExtraction:
    def test_structural_blocks_use_visual_coordinates(self, tmp_path):
        """
        On a /Rotate 90 page, an unrotated y would order blocks by their
        horizontal position, scrambling the page.
        """
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        # Visual (x, y) maps back to unrotated (y, page_height - x). The two
        # lines differ on both axes, so they stay distinct blocks and their
        # unrotated order is the reverse of their visual one.
        placements = [(120, 150, "Upper line here"), (420, 430, "Lower line here")]
        for visual_x, visual_y, text in placements:
            page.insert_text((visual_y, 792 - visual_x), text,
                             fontsize=11, fontname="helv", rotate=270)
        page.set_rotation(90)
        path = tmp_path / "rotated.pdf"
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            blocks = extractor.get_structural_blocks()

        assert len(blocks) == 2
        ordered = [b.text for b in sorted(blocks, key=lambda b: (b.y0, b.x0))]
        assert ordered == ["Upper line here", "Lower line here"]


class TestExhibitStrategy:
    def build_pdf(self, path):
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((80, 80), "Chapter 1 - Development", fontsize=14, fontname="hebo")
        page.insert_text((80, 110), "The table below shows reported claims by year.",
                         fontsize=11, fontname="helv")
        columns = [90, 220, 330, 440]
        for index, x in enumerate(columns):
            page.insert_text((x, 180), f"Col{index}", fontsize=9, fontname="helv")
        for row in range(6):
            y = 200 + row * 16
            page.insert_text((columns[0], y), f"200{row}", fontsize=9, fontname="helv")
            for col, x in enumerate(columns[1:], start=1):
                # Distinct value per cell, so a duplicate in the output means
                # the same number really was emitted twice.
                page.insert_text((x, y), f"{row + 1},{col}0{row}", fontsize=9, fontname="helv")
        doc.save(str(path))
        doc.close()

    def convert(self, tmp_path, strategy):
        pdf = tmp_path / "book.pdf"
        self.build_pdf(pdf)
        epub = tmp_path / f"{strategy}.epub"
        result = Converter(strategy=strategy).convert(pdf, epub)
        assert result.status in ("success", "warning"), result.log.errors
        with zipfile.ZipFile(epub) as archive:
            html = "".join(
                archive.read(name).decode("utf-8")
                for name in archive.namelist() if name.endswith(".xhtml")
            )
        return html

    def test_exhibit_strategy_emits_a_real_table(self, tmp_path):
        html = self.convert(tmp_path, "exhibit")
        assert "<table" in html and "<tbody>" in html
        assert "<td" in html

    def test_simple_strategy_still_emits_paragraphs_only(self, tmp_path):
        html = self.convert(tmp_path, "simple")
        assert "<table" not in html

    def test_table_values_are_not_also_emitted_as_paragraphs(self, tmp_path):
        html = self.convert(tmp_path, "exhibit")
        # Each value appears once, inside a cell, not again as loose text.
        assert html.count("1,100") == 1

    def test_prose_survives_alongside_the_table(self, tmp_path):
        html = self.convert(tmp_path, "exhibit")
        assert "reported claims by year" in html

    def test_table_stylesheet_travels_with_the_chapter(self, tmp_path):
        html = self.convert(tmp_path, "exhibit")
        assert "table.exhibit" in html

    def test_placeholder_carries_table_text(self):
        """Word counts and text-level checks must still see the cells."""
        from conversion.detectors.table_detector import Cell, DetectedTable
        table = DetectedTable(
            page=1, bbox=(0, 0, 10, 10),
            rows=[[Cell("Year"), Cell("48,953", numeric=True)]],
        )
        block = ExhibitStrategy._make_placeholder(0, 1, fitz.Rect(0, 0, 10, 10), table)
        assert isinstance(block, PlaceholderBlock)
        assert "48,953" in block.text
        # "digit + two spaces" is the endnote signature: never produce it.
        assert "  " not in block.text

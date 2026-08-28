"""Tests for paragraph segmentation and wide-table splitting.

Run from the skill directory:  python -m pytest tests/ -v
"""

import fitz
import pytest

from conversion.detectors.table_detector import Cell, DetectedTable
from conversion.detectors.table_renderer import TableRenderer
from core.pdf_extractor import PDFExtractor


def write_page(path, lines, size=11.0, bold=False):
    """Build a one-page PDF from (y, text) pairs."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    font = "hebo" if bold else "helv"
    for item in lines:
        y, text = item[0], item[1]
        page.insert_text((90, y), text, fontsize=item[2] if len(item) > 2 else size,
                         fontname=item[3] if len(item) > 3 else font)
    doc.save(str(path))
    doc.close()


class TestParagraphSegmentation:
    """
    PyMuPDF returns a block per region of the page, not per paragraph. Left
    alone, a prose page becomes one block spanning every paragraph and heading
    on it, and the whole chapter renders as a single enormous <p>.
    """

    def test_blank_line_separates_paragraphs(self, tmp_path):
        path = tmp_path / "gap.pdf"
        write_page(path, [
            (100, "First paragraph opening line continues here."),
            (114, "and wraps onto a second line of the same paragraph."),
            # A blank line's worth of space, then a new paragraph.
            (150, "Second paragraph starts after a clear vertical gap."),
            (164, "and it also wraps onto a second line here."),
        ])
        with PDFExtractor(path) as extractor:
            blocks = extractor.get_structural_blocks()

        assert len(blocks) == 2
        assert blocks[0].text.startswith("First paragraph")
        assert "wraps onto a second line of the same" in blocks[0].text
        assert blocks[1].text.startswith("Second paragraph")

    def test_heading_is_split_from_the_prose_around_it(self, tmp_path):
        path = tmp_path / "heading.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((90, 100), "Closing line of the preceding paragraph.",
                         fontsize=11, fontname="helv")
        page.insert_text((90, 130), "A Heading In Larger Bold Type",
                         fontsize=14, fontname="hebo")
        page.insert_text((90, 160), "Opening line of the following paragraph.",
                         fontsize=11, fontname="helv")
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            blocks = extractor.get_structural_blocks()

        texts = [b.text for b in blocks]
        assert "A Heading In Larger Bold Type" in texts
        heading = next(b for b in blocks if b.text == "A Heading In Larger Bold Type")
        assert heading.font_size > 11.0
        assert heading.flags & 16  # bold

    def test_indented_first_line_starts_a_paragraph(self, tmp_path):
        """Documents that indent instead of leaving a blank line."""
        path = tmp_path / "indent.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((90, 100), "First paragraph line one of the text.",
                         fontsize=11, fontname="helv")
        page.insert_text((90, 114), "still the first paragraph on line two.",
                         fontsize=11, fontname="helv")
        page.insert_text((115, 128), "Indented line begins a new paragraph.",
                         fontsize=11, fontname="helv")
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            blocks = extractor.get_structural_blocks()

        assert len(blocks) == 2
        assert blocks[1].text.startswith("Indented line")

    def test_superscript_note_marker_is_set_apart(self, tmp_path):
        """
        Footnote handling keys on whitespace that exists in the PDF only as
        typography: the marker is a small raised span carrying no spaces.
        """
        path = tmp_path / "note.pdf"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((90, 700), "6", fontsize=6.5, fontname="helv")
        page.insert_text((97, 700), "In a number of countries the rule differs.",
                         fontsize=10, fontname="helv")
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            blocks = extractor.get_structural_blocks()

        note = " ".join(b.text for b in blocks)
        # "digit followed by two spaces" is how an endnote start is recognised.
        assert "6  In a number of countries" in note

    def test_page_text_and_block_text_agree(self, tmp_path):
        """
        The extracted page text is the reference the conversion is validated
        against. If it segments differently from the blocks the EPUB is built
        from, ordinary prose reads as a mismatch.
        """
        path = tmp_path / "agree.pdf"
        write_page(path, [
            (100, "A paragraph of ordinary prose about the subject."),
            (140, "A second paragraph, clearly separated from the first."),
            (180, "A third paragraph rounding the page out nicely."),
        ])
        with PDFExtractor(path) as extractor:
            page_text = extractor.get_full_text()
            blocks = extractor.get_structural_blocks()

        for block in blocks:
            assert block.text in page_text


class TestWideTableSplitting:
    """
    E-ink readers do not scroll sideways: a table wider than the screen has its
    right-hand columns clipped off the page rather than made reachable.
    """

    def build(self, columns, value="123,456"):
        header = [Cell(f"C{i}", is_header=True) for i in range(columns)]
        rows = [header]
        for year in range(2000, 2006):
            row = [Cell(str(year), numeric=True)]
            row += [Cell(value, numeric=True) for _ in range(columns - 1)]
            rows.append(row)
        return DetectedTable(page=1, bbox=(0, 0, 100, 100), rows=rows, header_row_count=1)

    def test_narrow_table_is_not_split(self):
        html = TableRenderer(width_budget=105).render(self.build(4))
        assert html.count("<table") == 1
        assert "table-part-note" not in html

    def test_wide_table_is_split(self):
        html = TableRenderer(width_budget=105).render(self.build(14))
        assert html.count("<table") > 1
        assert "table-part-note" in html

    def test_every_value_survives_the_split(self):
        table = self.build(14)
        html = TableRenderer(width_budget=105).render(table)
        for row in table.rows:
            for cell in row:
                assert cell.text in html

    def test_row_labels_repeat_in_every_part(self):
        html = TableRenderer(width_budget=105).render(self.build(14))
        # The accident year names the row; without it a later part is unreadable.
        assert html.count("2003") >= 2

    def test_each_part_carries_the_header(self):
        html = TableRenderer(width_budget=105).render(self.build(14))
        assert html.count("<thead>") == html.count("<table")

    def test_narrow_values_allow_more_columns(self):
        """The budget is about width, not column count."""
        wide_values = TableRenderer(width_budget=105).render(self.build(9, "1,234,567,890"))
        narrow_values = TableRenderer(width_budget=105).render(self.build(9, "1.0"))
        assert wide_values.count("<table") > narrow_values.count("<table")

    def test_colspans_are_reformed_within_a_part(self):
        rows = [
            [Cell("Year", is_header=True), Cell("Group A", colspan=4, is_header=True),
             Cell("Group B", colspan=4, is_header=True)],
            [Cell("2001", numeric=True)] + [Cell("1,000,000", numeric=True) for _ in range(8)],
            [Cell("2002", numeric=True)] + [Cell("2,000,000", numeric=True) for _ in range(8)],
            [Cell("2003", numeric=True)] + [Cell("3,000,000", numeric=True) for _ in range(8)],
        ]
        table = DetectedTable(page=1, bbox=(0, 0, 100, 100), rows=rows, header_row_count=1)
        html = TableRenderer(width_budget=60).render(table)
        assert html.count("<table") > 1
        # A group header sliced across parts keeps a valid, smaller colspan.
        assert 'colspan="9"' not in html
        assert "Group A" in html

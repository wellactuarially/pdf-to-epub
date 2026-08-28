"""Tests for recovering chapter boundaries from running headers.

Run from the skill directory:  python -m pytest tests/ -v
"""

import re
import zipfile

import fitz
import pytest

from conversion.converter import Converter
from conversion.detectors.running_header import RunningHeaderDetector


WORDS = [
    "actuary", "triangle", "estimate", "reserve", "premium", "severity",
    "frequency", "development", "exposure", "valuation", "settlement",
    "reported", "outstanding", "ultimate", "adjustment", "portfolio",
    "liability", "underwriting", "experience", "projection", "diagnostic",
    "assumption", "methodology", "calibration", "aggregate", "deductible",
]


# Ten distinctly-named chapters: heads that share a suffix would collapse into
# one pattern when the noise check normalises digits away.
CHAPTERS = [(f"Chapter {i} - {WORDS[i].title()}", 4) for i in range(1, 11)]


def build_book(chapter_pages, book_title="A Book Of Things", rotate=False):
    """
    Build a PDF whose only chapter markers are its running headers.

    `chapter_pages` is a list of (head, page_count) pairs. Body text never
    names the chapter, which is exactly the case that defeats font-based
    heading detection.
    """
    doc = fitz.open()
    serial = 0
    for head, count in chapter_pages:
        for _ in range(count):
            page = doc.new_page(width=612, height=792)
            # Headers sit high enough to fall inside the header band on both
            # page orientations (a rotated page is only 612pt tall).
            lines = [(40, book_title), (56, head)]
            # Body text must differ page to page in its *words*: the noise
            # detector normalises digits away before comparing lines, so
            # numbering alone would still collapse to one repeated pattern
            # and the whole body would be stripped as a running header.
            body = [
                (140 + i * 24,
                 f"The {WORDS[i % len(WORDS)]} marker {chr(97 + serial % 26)}"
                 f"{chr(97 + i % 26)} concerns the {WORDS[(i + serial) % len(WORDS)]} "
                 f"and its {WORDS[(i * 3 + serial) % len(WORDS)]}.")
                for i in range(12)
            ]
            serial += 1
            for y, text in lines + body:
                if rotate:
                    # Visual (x, y) maps back to unrotated (y, page_height - x).
                    page.insert_text((y, 792 - 90), text, fontsize=10,
                                     fontname="helv", rotate=270)
                else:
                    page.insert_text((90, y), text, fontsize=10, fontname="helv")
            if rotate:
                page.set_rotation(90)
    return doc


class TestRunningHeaderDetector:
    def test_recovers_chapters(self):
        doc = build_book([("Chapter 1 - Beginnings", 5),
                          ("Chapter 2 - Middles", 6),
                          ("Chapter 3 - Ends", 4)])
        try:
            runs = RunningHeaderDetector().detect(doc)
            assert [r.title for r in runs] == [
                "Chapter 1 - Beginnings", "Chapter 2 - Middles", "Chapter 3 - Ends",
            ]
            assert [(r.first_page, r.last_page) for r in runs] == [(1, 5), (6, 11), (12, 15)]
        finally:
            doc.close()

    def test_book_title_does_not_divide_the_book(self):
        """The line on every page is the title, not a chapter marker."""
        doc = build_book([("Chapter 1 - One", 5), ("Chapter 2 - Two", 5)])
        try:
            runs = RunningHeaderDetector().detect(doc)
            assert all("A Book Of Things" not in r.title for r in runs)
            assert len(runs) == 2
        finally:
            doc.close()

    def test_merges_runs_naming_the_same_division(self):
        """
        Prose and exhibit pages of one chapter often word the head differently.
        They are still one chapter.
        """
        doc = build_book([
            ("Chapter 1 - Frequency-Severity Techniques", 4),
            ("Chapter 1 - Frequency-Severity Technique", 5),   # singular
            ("Chapter 2 – Case Outstanding", 4),               # en dash
            ("Chapter 2 - Case Outstanding", 4),               # hyphen
        ])
        try:
            runs = RunningHeaderDetector().detect(doc)
            assert len(runs) == 2
            # The first run's wording wins: it is the one the contents uses.
            assert runs[0].title == "Chapter 1 - Frequency-Severity Techniques"
            assert runs[0].page_count == 9
            assert runs[1].page_count == 8
        finally:
            doc.close()

    def test_rejects_weak_evidence(self):
        """One chapter is no structure; impose nothing rather than guess."""
        doc = build_book([("Chapter 1 - Only", 8)])
        try:
            assert RunningHeaderDetector().detect(doc) == []
        finally:
            doc.close()

    def test_rejects_when_headers_never_repeat(self):
        doc = fitz.open()
        for index in range(12):
            page = doc.new_page(width=612, height=792)
            page.insert_text((90, 60), f"Unique heading {index}", fontsize=10, fontname="helv")
            page.insert_text((90, 200), "Body text.", fontsize=10, fontname="helv")
        try:
            assert RunningHeaderDetector().detect(doc) == []
        finally:
            doc.close()

    def test_pages_without_a_head_join_the_previous_chapter(self):
        doc = build_book([("Chapter 1 - One", 4), ("Chapter 2 - Two", 4)])
        # A full-page figure with no running head, appended to chapter 2.
        page = doc.new_page(width=612, height=792)
        page.insert_text((90, 400), "A full page figure with no header.",
                         fontsize=10, fontname="helv")
        try:
            runs = RunningHeaderDetector().detect(doc)
            assert len(runs) == 2
            assert runs[-1].last_page == 9
        finally:
            doc.close()

    def test_rotated_pages(self):
        doc = build_book([("Chapter 1 - One", 5), ("Chapter 2 - Two", 5)], rotate=True)
        try:
            runs = RunningHeaderDetector().detect(doc)
            assert [r.title for r in runs] == ["Chapter 1 - One", "Chapter 2 - Two"]
        finally:
            doc.close()

    def test_max_chapters_guard(self):
        doc = build_book([(f"Chapter {i} - Section", 3) for i in range(1, 9)])
        try:
            assert RunningHeaderDetector(max_chapters=4).detect(doc) == []
        finally:
            doc.close()


class TestRunningHeadStripping:
    def test_chapter_head_is_kept_out_of_the_body(self, tmp_path):
        """
        A head naming one chapter repeats on too few of a long book's pages
        to trip the global noise threshold, so without special handling the
        chapter title is reprinted into the text at the top of every page.
        """
        from core.pdf_extractor import PDFExtractor

        # Ten chapters with distinct names, so no head reaches the global
        # noise threshold of one page in five, and none of them collapse
        # together when that check normalises digits away. This is the
        # situation in a real book.
        path = tmp_path / "book.pdf"
        doc = build_book(CHAPTERS)
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            text = extractor.get_full_text()
            blocks = extractor.get_structural_blocks()

        assert CHAPTERS[0][0] not in text
        assert not any(CHAPTERS[6][0] in b.text for b in blocks)
        # The body itself must survive.
        assert "concerns the" in text

    def test_body_text_below_the_header_band_is_untouched(self, tmp_path):
        from core.pdf_extractor import PDFExtractor

        doc = build_book(CHAPTERS)
        # One page also mentions a chapter by name down in the body. That is a
        # citation, not a running head, and must survive intact.
        doc[3].insert_text((90, 620),
                           f"{CHAPTERS[0][0]} is discussed at length here.",
                           fontsize=10, fontname="helv")
        path = tmp_path / "cited.pdf"
        doc.save(str(path))
        doc.close()

        with PDFExtractor(path) as extractor:
            text = extractor.get_full_text()

        # The mid-page mention survives intact; only the head itself goes.
        assert f"{CHAPTERS[0][0]} is discussed at length here" in text
        assert f"{CHAPTERS[0][0]}\n" not in text


class TestChapterSplittingEndToEnd:
    @pytest.fixture
    def book(self, tmp_path):
        path = tmp_path / "book.pdf"
        doc = build_book([("Chapter 1 - Beginnings", 5),
                          ("Chapter 2 - Middles", 5),
                          ("Chapter 3 - Ends", 5)])
        doc.save(str(path))
        doc.close()
        return path

    def convert(self, book, tmp_path, config=None):
        epub = tmp_path / "out.epub"
        Converter(strategy="simple").convert(book, epub, config=config)
        archive = zipfile.ZipFile(epub)
        chapters = [n for n in archive.namelist() if n.endswith(".xhtml")]
        return archive, chapters

    def test_one_file_per_chapter(self, book, tmp_path):
        archive, chapters = self.convert(book, tmp_path)
        titles = sorted(
            re.search(r"<title>(.*?)</title>", archive.read(n).decode("utf-8"), re.S).group(1).strip()
            for n in chapters
        )
        for expected in ["Chapter 1 - Beginnings", "Chapter 2 - Middles", "Chapter 3 - Ends"]:
            assert expected in titles

    def test_navigation_lists_the_chapters(self, book, tmp_path):
        archive, _ = self.convert(book, tmp_path)
        ncx = archive.read("OEBPS/toc.ncx").decode("utf-8")
        assert ncx.count("<navPoint") >= 3
        assert "Chapter 2 - Middles" in ncx

    def test_can_be_switched_off(self, book, tmp_path):
        from dataclasses import replace
        from conversion.converter import DEFAULT_CONFIG
        config = replace(
            DEFAULT_CONFIG,
            chapter_detection=replace(DEFAULT_CONFIG.chapter_detection,
                                      use_running_headers=False),
        )
        archive, chapters = self.convert(book, tmp_path, config=config)
        titles = " ".join(
            re.search(r"<title>(.*?)</title>", archive.read(n).decode("utf-8"), re.S).group(1)
            for n in chapters
        )
        assert "Chapter 2 - Middles" not in titles

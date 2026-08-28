# ADAPTABLE: Running-header heuristics can be tuned
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Recovers chapter boundaries from running headers.

Many technical books never print the chapter title in the body text. The only
place "Chapter 5 - The Development Triangle" appears is the running head at
the top of every page — which header/footer stripping correctly removes,
because repeating it in the flow would be noise. The consequence is a book
with no detectable chapter starts at all: everything lands in one enormous
XHTML file, the table of contents is empty, and the reader cannot navigate.

This detector reads those running heads before they are stripped, and turns
each run of consecutive pages sharing a head into a chapter.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import fitz

from core.utils import get_logger

logger = get_logger(__name__)

# Running heads that name a division of the book. Checked first: a book's
# header band often also carries an exhibit or section label, and the
# structural one is the one that says so.
CHAPTER_PATTERN = re.compile(
    r"^\s*(chapter|part|appendix|annex|section|book)\b",
    re.IGNORECASE,
)

# The stable part of a running head: "Chapter 11", "Appendix A". The prose and
# exhibit pages of one chapter often word the rest of the head differently —
# singular against plural, en dash against hyphen, sometimes an entirely
# different subtitle — so the identifier is what decides whether two runs are
# the same division of the book.
DIVISION_PATTERN = re.compile(
    r"^\s*(chapter|part|appendix|annex|section|book)\s*"
    r"([0-9]+|[ivxlcIVXLC]+|[A-Z])\b",
    re.IGNORECASE,
)


@dataclass
class ChapterRun:
    """A run of consecutive pages sharing one running head."""
    title: str
    first_page: int
    last_page: int

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


class RunningHeaderDetector:
    """Derives chapter boundaries from repeated page headers."""

    def __init__(
        self,
        header_zone: float = 0.12,
        min_occurrences: int = 3,
        universal_ratio: float = 0.8,
        min_chapters: int = 2,
        max_chapters: int = 80,
    ):
        """
        Args:
            header_zone: Fraction of page height treated as the header band.
            min_occurrences: Pages a line must appear on to count as a
                running head rather than body text.
            universal_ratio: A line appearing on more than this share of pages
                is the book title, which does not divide anything.
            min_chapters: Below this, assume the detection failed and return
                nothing rather than imposing a bad structure.
            max_chapters: Above this, the "header" is probably varying text.
        """
        self.header_zone = header_zone
        self.min_occurrences = min_occurrences
        self.universal_ratio = universal_ratio
        self.min_chapters = min_chapters
        self.max_chapters = max_chapters

        # Populated by detect(): the chapter head printed on each page, and
        # where that page's header band ends. A chapter-scoped head like
        # "Chapter 1 - Overview" appears on too few of the book's pages for
        # global noise stripping to catch it, so it otherwise survives into
        # the body — repeated at the top of every page of the chapter. Note
        # this holds the literal head printed on that page, which may differ
        # in wording from the merged chapter title.
        self.page_titles: Dict[int, str] = {}
        self.header_limits: Dict[int, float] = {}

    # ---------------------------------------------------------------- public

    def detect(self, doc: "fitz.Document") -> List[ChapterRun]:
        """
        Find chapter runs in a document.

        Returns an empty list when the evidence is weak, so a book that really
        does print its chapter titles is left to the normal heading detector.
        """
        per_page = self._header_lines(doc)
        if not per_page:
            return []

        frequency = Counter(text for lines in per_page.values() for text in set(lines))
        page_count = len(doc)
        universal = {
            text for text, count in frequency.items()
            if count > page_count * self.universal_ratio
        }

        titles = self._page_titles(per_page, frequency, universal)
        if not titles:
            return []

        runs = self._merge_divisions(self._runs(titles, page_count))
        if not (self.min_chapters <= len(runs) <= self.max_chapters):
            logger.info(
                f"Running-header chapters rejected: {len(runs)} runs outside "
                f"[{self.min_chapters}, {self.max_chapters}]"
            )
            return []

        self.page_titles = dict(titles)

        logger.info(f"Recovered {len(runs)} chapters from running headers")
        return runs

    # ------------------------------------------------------------- internals

    def _header_lines(self, doc: "fitz.Document") -> Dict[int, List[str]]:
        """Text lines sitting in each page's header band, in visual space."""
        per_page: Dict[int, List[str]] = {}
        for page in doc:
            matrix = page.rotation_matrix
            limit = page.rect.height * self.header_zone
            lines: List[Tuple[float, str]] = []

            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    box = fitz.Rect(line.get("bbox", (0, 0, 0, 0))) * matrix
                    if box.y1 > limit:
                        continue
                    text = "".join(
                        span.get("text", "") for span in line.get("spans", [])
                    ).strip()
                    if len(text) >= 4:
                        lines.append((box.y0, text))

            self.header_limits[page.number + 1] = limit
            if lines:
                per_page[page.number + 1] = [text for _, text in sorted(lines)]
        return per_page

    def _page_titles(
        self,
        per_page: Dict[int, List[str]],
        frequency: Counter,
        universal: set,
    ) -> Dict[int, str]:
        """Pick the one header line per page that names the chapter."""
        candidates: Dict[int, str] = {}
        for page_number, lines in per_page.items():
            usable = [
                text for text in lines
                if text not in universal and frequency[text] >= self.min_occurrences
            ]
            if not usable:
                continue

            # Prefer an explicit "Chapter N" / "Appendix A" style head; fall
            # back to the most repeated line, which is the running head in
            # books that name their chapters without the word "Chapter".
            structural = [text for text in usable if CHAPTER_PATTERN.match(text)]
            pool = structural or usable
            candidates[page_number] = max(pool, key=lambda text: frequency[text])

        # If any page produced a structural head, trust only those: a mix of
        # structural and incidental heads would fragment the book.
        structural_pages = {
            page: title for page, title in candidates.items()
            if CHAPTER_PATTERN.match(title)
        }
        return structural_pages or candidates

    @staticmethod
    def _runs(titles: Dict[int, str], page_count: int) -> List[ChapterRun]:
        """
        Turn per-page titles into runs of consecutive pages.

        Pages without a detected head — a chapter's opening page, a full-page
        figure — inherit the previous page's chapter rather than breaking it.
        """
        runs: List[ChapterRun] = []
        current: Optional[str] = None

        for page in range(1, page_count + 1):
            title = titles.get(page)
            if title is None:
                if runs:
                    runs[-1].last_page = page
                continue
            if title != current:
                runs.append(ChapterRun(title=title, first_page=page, last_page=page))
                current = title
            else:
                runs[-1].last_page = page

        return runs

    @staticmethod
    def _division_key(title: str) -> str:
        """
        Identity of the book division a running head names.

        Falls back to the whole head, with dashes and spacing normalised, for
        books whose heads carry no "Chapter N" identifier.
        """
        match = DIVISION_PATTERN.match(title)
        if match:
            return f"{match.group(1).lower()} {match.group(2).lower()}"
        normalised = re.sub(r"[–—-]", "-", title.lower())
        return " ".join(normalised.split())

    def _merge_divisions(self, runs: List[ChapterRun]) -> List[ChapterRun]:
        """
        Join adjacent runs that name the same division.

        The first run's title wins: a chapter's opening prose pages carry the
        head the table of contents uses, while later exhibit pages may carry
        an abbreviated or reworded one.
        """
        merged: List[ChapterRun] = []
        for run in runs:
            if merged and self._division_key(merged[-1].title) == self._division_key(run.title):
                merged[-1].last_page = run.last_page
                continue
            merged.append(ChapterRun(
                title=" ".join(run.title.split()),
                first_page=run.first_page,
                last_page=run.last_page,
            ))
        return merged

    @staticmethod
    def chapter_starts(runs: List[ChapterRun]) -> Dict[int, str]:
        """Map each chapter's first page to its title."""
        return {run.first_page: run.title for run in runs}

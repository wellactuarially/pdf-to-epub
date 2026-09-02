"""
PDF Extraction module using PyMuPDF (fitz) to extract text content.
Integrates with text_canonicalizer for clean output.
"""

import re
import statistics

import fitz
from pathlib import Path
from collections import Counter
from typing import Iterator, List, Dict, Any, Optional

from .utils import get_logger
from validation.text_canonicalizer import canonicalize

logger = get_logger(__name__)

# Running heads that name a division of the book. These repeat only across one
# chapter's pages, so the global noise threshold never catches them, yet a
# chapter title reprinted at the top of every page is plainly not body text.
_CHAPTER_HEAD = re.compile(
    r"^\s*(chapter|part|appendix|annex|section|book)\b.{0,90}$",
    re.IGNORECASE | re.DOTALL,
)


class PDFExtractor:
    """
    Handles PDF document reading and text extraction.
    Supports memory-efficient page iteration and automatic text canonicalization.
    """

    # Fraction of page height treated as the header band.
    HEADER_BAND = 0.12
    # Pages a chapter running head must appear on before it counts as one.
    CHAPTER_HEAD_MIN_PAGES = 3

    
    def __init__(self, file_path: Path):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"PDF file not found: {self.file_path}")
        
        self.doc: Optional[fitz.Document] = None
        self.noise_patterns: List[str] = []
        # Applied only inside the header band, so a chapter title mentioned in
        # the body ("see Chapter 5 - The Development Triangle") is preserved.
        self.header_patterns: List[str] = []
        self.body_font_size: Optional[float] = None

    def __enter__(self):
        try:
            self.doc = fitz.open(self.file_path)
            if self.doc.is_encrypted:
                logger.error(f"File {self.file_path.name} is encrypted and cannot be processed.")
                raise RuntimeError("Encrypted PDF files are not supported.")
            
            # Automatically identify noise patterns (headers/footers)
            self._identify_noise_patterns()
            
            return self
        except Exception as e:
            logger.error(f"Failed to open PDF {self.file_path}: {str(e)}")
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.doc:
            self.doc.close()

    def get_metadata(self) -> Dict[str, Any]:
        """
        Returns document metadata (Title, Author, etc.)
        """
        if not self.doc:
            raise RuntimeError("Document is not open. Use with context manager.")
        return self.doc.metadata

    def _identify_noise_patterns(self):
        """
        Scans the document to find recurring lines (headers, footers, copyrights).
        """
        if not self.doc or len(self.doc) < 2:
            return

        line_counts = Counter()
        font_size_counts = Counter()
        chapter_head_counts = Counter()
        # Sample up to 100 pages for the general statistics. Chapter heads are
        # counted across the whole book: a head belonging to a late chapter
        # appears nowhere in the first hundred pages.
        sample_size = 100
        sample_pages = self.doc[:sample_size]

        for page_index, page in enumerate(self.doc):
            in_sample = page_index < sample_size
            page_dict = page.get_text("dict")
            rotation_matrix = page.rotation_matrix
            header_limit = page.rect.height * self.HEADER_BAND
            seen_on_page = set()
            for b in page_dict.get("blocks", []):
                if b.get("type") != 0:
                    continue
                for line in b.get("lines", []):
                    spans = line.get("spans", [])
                    line_text = "".join(span.get("text", "") for span in spans).strip()
                    if not line_text:
                        continue

                    # A running head naming one chapter repeats on that
                    # chapter's pages only, which is too small a share of a
                    # long book to look like noise globally. Count those
                    # separately so they can still be stripped.
                    line_box = fitz.Rect(line.get("bbox", (0, 0, 0, 0))) * rotation_matrix
                    if line_box.y1 <= header_limit and _CHAPTER_HEAD.match(line_text):
                        chapter_head_counts[line_text] += 1

                    if not in_sample:
                        continue
                    for span in spans:
                        span_text = span.get("text", "")
                        if not span_text.strip():
                            continue
                        font_size_counts[round(span.get("size", 0.0), 1)] += len(span_text)
                    trimmed = line_text.strip()
                    if len(trimmed) < 10:
                        continue
                    # Normalize digits to # to catch page numbers
                    normalized = re.sub(r'\d+', '#', trimmed)
                    if normalized not in seen_on_page:
                        line_counts[normalized] += 1
                        seen_on_page.add(normalized)

        # A line is noise if it appears in more than 20% of sampled pages
        threshold = max(2, len(sample_pages) * 0.2)
        self.noise_patterns = [
            # We use regex to match the pattern (replacing # back with \d+)
            re.compile(re.escape(p).replace("\\#", r"\d+"))
            for p, count in line_counts.items() if count >= threshold
        ]

        # Chapter running heads need far less support: three pages carrying
        # the same "Chapter N - Title" at the very top is already a running
        # head, not prose that happens to repeat.
        self.header_patterns = [
            re.compile(re.escape(text))
            for text, count in chapter_head_counts.items()
            if count >= self.CHAPTER_HEAD_MIN_PAGES
        ]

        if font_size_counts:
            self.body_font_size = font_size_counts.most_common(1)[0][0]
        
        if self.noise_patterns or self.header_patterns:
            logger.info(
                f"Identified {len(self.noise_patterns)} noise patterns and "
                f"{len(self.header_patterns)} running heads in {self.file_path.name}"
            )

    def _extract_page_content(self, page: fitz.Page) -> str:
        """
        Extracts and cleans text from a single page, stripping embedded noise.
        """
        page_dict = page.get_text("dict")
        page_height = page.rect.height
        rotation_matrix = page.rotation_matrix

        # Blocks arrive in the PDF's internal order; sort them into visual reading
        # order so the extracted text is a stable reference for validation.
        ordered_blocks = sorted(
            (b for b in page_dict.get("blocks", []) if b.get("type") == 0),
            key=lambda b: self._visual_key(b.get("bbox", (0, 0, 0, 0)), rotation_matrix)
        )

        # Segment into paragraphs exactly as get_structural_blocks does. The two
        # must agree: this text is the reference the conversion is validated
        # against, and if one side groups by block while the other groups by
        # paragraph, every comparison near a boundary reads as a mismatch.
        page_text = []
        for b in ordered_blocks:
            for group in self._paragraph_groups(b, rotation_matrix):
                block_text = group["text"]
                if not block_text.strip():
                    continue

                if self._is_header_footer_line(
                    block_text, group["y0"], group["y1"], page_height, group["font"][1]
                ):
                    continue

                for pattern in self.noise_patterns:
                    block_text = pattern.sub("", block_text)
                if group["y0"] <= page_height * self.HEADER_BAND:
                    for pattern in self.header_patterns:
                        block_text = pattern.sub("", block_text)

                block_text = block_text.strip()
                if block_text:
                    page_text.append(block_text)

        raw_text = "\n\n".join(page_text)
        return canonicalize(raw_text)

    def iter_pages(self) -> Iterator[str]:
        """
        Yields canonicalized text for each page one by one.
        """
        if not self.doc:
            raise RuntimeError("Document is not open. Use with context manager.")
        
        for i, page in enumerate(self.doc):
            logger.debug(f"Extracting page {i+1}/{len(self.doc)}")
            yield self._extract_page_content(page)

    def get_full_text(self) -> str:
        """
        Returns the entire document text as a single canonicalized string.
        """
        return "\n\n".join(list(self.iter_pages()))

    @property
    def page_count(self) -> int:
        return len(self.doc) if self.doc else 0

    def get_structural_blocks(self):
        """
        Extracts blocks with detailed font information for structure detectors.
        Uses 'dict' output from PyMuPDF.
        """
        from conversion.detectors.models import TextBlock
        
        if not self.doc:
            raise RuntimeError("Document is not open.")
            
        all_blocks = []
        
        for page_num, page in enumerate(self.doc):
            page_height = page.rect.height
            # Text coordinates come back in unrotated page space, while page.rect
            # already reflects /Rotate. Map bboxes through the rotation matrix so
            # y really means "down the page as the reader sees it".
            rotation_matrix = page.rotation_matrix
            # "dict" format gives structure: block -> lines -> spans -> chars
            # flags decoding: 2^0=unused, 2^1=italic, 2^2=serif, 2^3=monospace, 2^4=bold
            blocks = page.get_text("dict")["blocks"]
            
            for b in blocks:
                if b["type"] != 0: # 0 = Text, 1 = Image
                    continue

                # A PyMuPDF block is a region of the page, not a paragraph: on a
                # prose page it routinely covers the whole text area, headings
                # included. Emitting one TextBlock per block would give every
                # downstream stage a single unit spanning a dozen paragraphs and
                # several headings, which is why long chapters used to render as
                # one enormous <p>. Split it first.
                for group in self._paragraph_groups(b, rotation_matrix):
                    block_text = group["text"]
                    x0, y0, x1, y1 = group["x0"], group["y0"], group["x1"], group["y1"]
                    dom_font, dom_size = group["font"]
                    dom_flags = group["flags"]

                    if self._is_header_footer_line(block_text, y0, y1, page_height, dom_size):
                        continue

                    for pattern in self.noise_patterns:
                        block_text = pattern.sub("", block_text)
                    if y0 <= page_height * self.HEADER_BAND:
                        for pattern in self.header_patterns:
                            block_text = pattern.sub("", block_text)
                    block_text = canonicalize(block_text)
                    if not block_text.strip():
                        continue

                    text_block = TextBlock(
                        text=block_text.strip(),
                        page=page_num + 1, # 1-based indexing for humans
                        x0=x0, y0=y0, x1=x1, y1=y1,
                        font_name=dom_font,
                        font_size=dom_size,
                        flags=dom_flags
                    )
                    all_blocks.append(text_block)
                
        return all_blocks

    # A line starting this much further down than the usual line pitch begins a
    # new paragraph rather than continuing the current one.
    PARAGRAPH_PITCH_FACTOR = 1.5
    # A first line indented by more than this many times the font size begins a
    # new paragraph, for documents that indent instead of leaving a blank line.
    PARAGRAPH_INDENT_FACTOR = 1.2

    def _paragraph_groups(self, block: dict, rotation_matrix) -> List[Dict[str, Any]]:
        """
        Split one PyMuPDF block into paragraph-sized groups of lines.

        A group ends at a blank line, at a change of type style, at an
        unusually large step down the page, or at an indented first line —
        the four ways a document signals "new paragraph". Splitting on type
        style is what lifts a heading out of the surrounding prose: it is
        set in a different size or weight, so it becomes its own group and
        the classifier can recognise it.

        Returns:
            One dict per paragraph with its text, visual bbox, dominant font
            and dominant flags.
        """
        lines = []
        for line in block.get("lines", []):
            rect = fitz.Rect(line.get("bbox", (0, 0, 0, 0))) * rotation_matrix
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                lines.append({"blank": True, "rect": rect})
                continue

            fonts: Counter = Counter()
            flags: Counter = Counter()
            for span in spans:
                weight = len(span.get("text", ""))
                fonts[(span.get("font", "Unknown"), span.get("size", 0.0))] += weight
                flags[span.get("flags", 0)] += weight

            dominant_size = fonts.most_common(1)[0][0][1]
            lines.append({
                "blank": False,
                "rect": rect,
                "text": self._join_spans(spans, dominant_size),
                "fonts": fonts,
                "flags": flags,
                "font": fonts.most_common(1)[0][0],
                "flag": flags.most_common(1)[0][0],
            })

        content = [line for line in lines if not line["blank"]]
        if not content:
            return []

        pitch = self._median_pitch(content)

        groups: List[List[dict]] = []
        current: List[dict] = []
        previous = None

        for line in lines:
            if line["blank"]:
                if current:
                    groups.append(current)
                current, previous = [], None
                continue

            if current and previous is not None and self._starts_paragraph(line, previous, current, pitch):
                groups.append(current)
                current = []

            current.append(line)
            previous = line

        if current:
            groups.append(current)

        return [self._as_group(g) for g in groups if g]

    @staticmethod
    def _join_spans(spans: List[dict], dominant_size: float) -> str:
        """
        Join a line's spans, setting superscript note numbers apart with spaces.

        Footnote handling downstream keys on whitespace: a reference is spotted
        as a number after punctuation and a space, and a note's own start as a
        number followed by two spaces. In the PDF that separation is
        typographic — the marker is a small raised span carrying no spaces at
        all — so it is restored here.

        The alternative, spacing every span, is what the upstream code did; it
        also works, but it drops a stray space either side of every italic
        phrase, which then makes the extracted page text disagree with the
        block text and turns ordinary prose into fuzzy matches during
        validation.
        """
        parts: List[str] = []
        after_marker = False
        for span in spans:
            text = span.get("text", "")
            marker = text.strip()
            is_superscript = (
                marker.isdigit()
                and len(marker) <= 3
                and span.get("size", 0.0) < dominant_size * 0.85
            )
            if is_superscript:
                # One space before, two after: the two-space gap is what marks
                # a note's own start, and it reads as a single gap either way.
                parts.append(f" {marker}  ")
                after_marker = True
                continue

            # The PDF may or may not put a space at the start of the text that
            # follows a marker; normalise so the gap is always exactly two.
            parts.append(text.lstrip() if after_marker else text)
            after_marker = False

        return "".join(parts).strip()

    @staticmethod
    def _median_pitch(content: List[dict]) -> float:
        """Typical baseline-to-baseline step, used to spot paragraph breaks."""
        steps = [
            content[i + 1]["rect"].y0 - content[i]["rect"].y0
            for i in range(len(content) - 1)
        ]
        steps = [s for s in steps if s > 0]
        if steps:
            return statistics.median(steps)
        return max(content[0]["rect"].height, 1.0) * 1.25

    def _starts_paragraph(self, line: dict, previous: dict, current: List[dict], pitch: float) -> bool:
        size = max(line["font"][1], 1.0)

        # A change of size or weight: a heading, or the note under a table.
        same_size = abs(line["font"][1] - previous["font"][1]) < 0.4
        same_weight = (line["flag"] & 16) == (previous["flag"] & 16)
        if not (same_size and same_weight):
            return True

        if line["rect"].y0 - previous["rect"].y0 > pitch * self.PARAGRAPH_PITCH_FACTOR:
            return True

        left = min(item["rect"].x0 for item in current)
        if line["rect"].x0 - left > size * self.PARAGRAPH_INDENT_FACTOR:
            return True

        return False

    @staticmethod
    def _as_group(lines: List[dict]) -> Dict[str, Any]:
        fonts: Counter = Counter()
        flags: Counter = Counter()
        for line in lines:
            fonts.update(line["fonts"])
            flags.update(line["flags"])

        return {
            # One form for both consumers: the page text used as the validation
            # reference and the block text used to build the EPUB must agree,
            # or every comparison near a span boundary reads as a mismatch.
            "text": " ".join(line["text"] for line in lines if line["text"]),
            "x0": min(line["rect"].x0 for line in lines),
            "y0": min(line["rect"].y0 for line in lines),
            "x1": max(line["rect"].x1 for line in lines),
            "y1": max(line["rect"].y1 for line in lines),
            "font": fonts.most_common(1)[0][0] if fonts else ("Unknown", 0.0),
            "flags": flags.most_common(1)[0][0] if flags else 0,
        }

    @staticmethod
    def _visual_key(bbox, rotation_matrix):
        """Sort key placing a bbox in visual reading order (top-to-bottom, left-to-right)."""
        rect = fitz.Rect(bbox) * rotation_matrix
        return (round(rect.y0, 1), round(rect.x0, 1))

    def _is_header_footer_line(
        self,
        text: str,
        y0: float,
        y1: float,
        page_height: float,
        font_size: float
    ) -> bool:
        if not self._is_header_footer_position(y0, y1, page_height):
            return False
        if self.body_font_size and font_size >= self.body_font_size * 1.2:
            return False
        return self._looks_like_header_footer(text)

    def _is_header_footer_position(self, y0: float, y1: float, page_height: float) -> bool:
        if page_height <= 0:
            return False
        top_limit = page_height * 0.08
        bottom_limit = page_height * 0.92
        return y1 <= top_limit or y0 >= bottom_limit

    def _looks_like_header_footer(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if re.fullmatch(r"[\d\W]+", stripped):
            return True

        letters = [ch for ch in stripped if ch.isalpha()]
        upper = [ch for ch in letters if ch.isupper()]
        upper_ratio = (len(upper) / len(letters)) if letters else 0.0

        digits = sum(1 for ch in stripped if ch.isdigit())
        digit_ratio = digits / len(stripped)
        word_count = len(stripped.split())

        if digits and digit_ratio >= 0.3 and word_count <= 8:
            return True
        if digits and upper_ratio >= 0.6 and word_count <= 10:
            return True
        if upper_ratio >= 0.75 and len(stripped) <= 80:
            return True

        return False

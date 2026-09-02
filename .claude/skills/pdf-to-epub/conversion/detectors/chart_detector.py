# ADAPTABLE: Chart detection heuristics can be tuned
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Finds charts drawn as vector graphics and turns them into raster figures.

Line and bar charts exported from spreadsheets carry no embedded image: they
are paths, so plain image extraction returns nothing and the axis labels leak
into the text flow as stray fragments. This detector clusters drawing
operations into regions, separates plots from ruled tables by how densely
text sits inside the region, and renders each plot to a PNG that can be
embedded like any other figure.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import fitz

from core.utils import get_logger

logger = get_logger(__name__)


@dataclass
class DetectedChart:
    """A vector-graphic region rendered to a raster image."""
    page: int
    bbox: Tuple[float, float, float, float]
    data: bytes
    width: int
    height: int
    caption: Optional[str] = None
    labels: Tuple[str, ...] = ()

    @property
    def rect(self) -> fitz.Rect:
        return fitz.Rect(*self.bbox)


class ChartDetector:
    """Detects vector chart regions and rasterizes them."""

    def __init__(
        self,
        min_area_ratio: float = 0.10,
        max_text_density: float = 3.5,
        min_drawings: int = 8,
        dpi: int = 150,
        merge_padding: float = 12.0,
    ):
        """
        Args:
            min_area_ratio: Smallest region area, as a share of the page.
            max_text_density: Words per 100pt² above which a region is treated
                as a ruled table rather than a plot. Charts hold only axis
                ticks and a legend; tables are dense with numbers.
            min_drawings: Minimum drawing operations in a region.
            dpi: Rendering resolution for the extracted figure.
            merge_padding: Gap (pt) within which drawings join one region.
        """
        self.min_area_ratio = min_area_ratio
        self.max_text_density = max_text_density
        self.min_drawings = min_drawings
        self.dpi = dpi
        self.merge_padding = merge_padding

    def detect(self, page: "fitz.Page") -> List[DetectedChart]:
        """Detect and render every chart on a page, in reading order."""
        matrix = page.rotation_matrix
        rects = []
        for drawing in page.get_drawings():
            rect = fitz.Rect(drawing["rect"]) * matrix
            if rect.width < 1 and rect.height < 1:
                continue
            # Count the strokes in a path, not the path: a whole plot can be
            # committed as a single path holding dozens of segments.
            rects.append((rect, max(1, len(drawing.get("items", ())))))

        if sum(weight for _, weight in rects) < self.min_drawings:
            return []

        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return []

        words = [
            (fitz.Rect(w[:4]) * matrix, w[4])
            for w in page.get_text("words") if w[4].strip()
        ]

        charts = []
        for region, count in self._merge_regions(rects):
            area = region.get_area()
            if area < page_area * self.min_area_ratio or count < self.min_drawings:
                continue

            inside = [(r, t) for r, t in words if region.contains(r)]
            density = len(inside) / (area / 10000.0)
            if density > self.max_text_density:
                continue  # dense with text: a ruled table, not a plot

            chart = self._render(page, region, inside)
            if chart is not None:
                charts.append(chart)

        charts.sort(key=lambda c: (c.bbox[1], c.bbox[0]))
        return charts

    def _merge_regions(self, rects: List[Tuple[fitz.Rect, int]]) -> List[Tuple[fitz.Rect, int]]:
        """Union drawing rectangles that touch or nearly touch, summing weights."""
        regions = [(fitz.Rect(r), weight) for r, weight in rects]
        pad = self.merge_padding

        merged = True
        while merged:
            merged = False
            for i in range(len(regions)):
                for j in range(i + 1, len(regions)):
                    a, count_a = regions[i]
                    b, count_b = regions[j]
                    if not (a & (b + (-pad, -pad, pad, pad))).is_empty:
                        regions[i] = (a | b, count_a + count_b)
                        regions.pop(j)
                        merged = True
                        break
                if merged:
                    break
        return regions

    def _render(
        self,
        page: "fitz.Page",
        region: fitz.Rect,
        inside: List[Tuple[fitz.Rect, str]]
    ) -> Optional[DetectedChart]:
        """Rasterize a chart region, clipped to the page."""
        clip = (region + (-6, -6, 6, 6)) & page.rect
        if clip.is_empty or clip.width < 20 or clip.height < 20:
            return None

        try:
            pixmap = page.get_pixmap(dpi=self.dpi, clip=clip)
            data = pixmap.tobytes("png")
        except Exception as exc:
            logger.warning(f"Could not render chart on page {page.number + 1}: {exc}")
            return None

        labels = tuple(text for _, text in sorted(inside, key=lambda item: (item[0].y0, item[0].x0)))
        return DetectedChart(
            page=page.number + 1,
            bbox=(clip.x0, clip.y0, clip.x1, clip.y1),
            data=data,
            width=pixmap.width,
            height=pixmap.height,
            labels=labels,
        )

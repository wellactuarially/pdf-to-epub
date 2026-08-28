# ADAPTABLE: Can be extended or used as template for new strategies
# See ~/.claude/skills/pdf-to-epub/reference/code-adaptation.md
"""
Conversion strategy for books whose substance lives in tables and charts.

``SimpleStrategy`` renders every text block as a paragraph, which is right for
a novel and wrong for a technical book: a claim development triangle arrives as
a few hundred loose numbers with no columns, and a chart drawn as vector paths
disappears entirely, leaving its axis labels stranded in the prose.

This strategy adds two passes before the usual flow:

* tables are reconstructed into cell grids and rendered as real ``<table>``
  markup, and the text blocks they consumed are replaced by a placeholder so
  the same numbers are not also emitted as paragraphs;
* vector charts are rasterized into figures, and their axis labels are removed
  from the text flow for the same reason.

Everything else — reading order, heading detection, chapter building, endnote
linking — is inherited unchanged.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz

from .simple_strategy import SimpleStrategy
from conversion.models import BookMetadata, ImageResource
from conversion.detectors.models import TextBlock
from conversion.detectors.chart_detector import ChartDetector, DetectedChart
from conversion.detectors.table_detector import DetectedTable, TableDetector
from conversion.detectors.table_renderer import TABLE_STYLESHEET, TableRenderer
from core.utils import get_logger

logger = get_logger(__name__)


class PlaceholderBlock(TextBlock):
    """
    A text block standing in for a table.

    Carrying the table through the pipeline as an ordinary block means reading
    order, chapter assignment and endnote handling all keep working; the
    renderer swaps in the real markup at the end.
    """

    def __init__(self, table_index: int, **kwargs):
        super().__init__(**kwargs)
        self.table_index = table_index


class ExhibitStrategy(SimpleStrategy):
    """Conversion strategy for table-heavy and chart-heavy documents."""

    def __init__(self):
        super().__init__()
        self.tables: List[DetectedTable] = []
        self.charts: List[DetectedChart] = []
        self.renderer = TableRenderer()
        self._chart_captions: Dict[str, str] = {}

    # ---------------------------------------------------------------- extract

    def extract(self, pdf_path: Path, config) -> Tuple[List[TextBlock], List[ImageResource], BookMetadata]:
        blocks, images, metadata = super().extract(pdf_path, config)

        table_config = getattr(config, "table_extraction", None)
        chart_config = getattr(config, "chart_extraction", None)
        tables_enabled = getattr(table_config, "enabled", True)
        charts_enabled = getattr(chart_config, "enabled", True)

        if not tables_enabled and not charts_enabled:
            return blocks, images, metadata

        detector = self._build_table_detector(table_config)
        chart_detector = self._build_chart_detector(chart_config)

        self.tables = []
        self.charts = []
        covered: List[Tuple[int, fitz.Rect]] = []
        placeholders: List[PlaceholderBlock] = []

        doc = fitz.open(str(pdf_path))
        try:
            for page in doc:
                page_number = page.number + 1

                page_charts = chart_detector.detect(page) if charts_enabled else []
                for chart in page_charts:
                    self.charts.append(chart)
                    covered.append((page_number, chart.rect))

                if not tables_enabled:
                    continue

                exclude = [c.rect for c in page_charts]
                for table in detector.detect(page, exclude=exclude):
                    index = len(self.tables)
                    self.tables.append(table)
                    rect = fitz.Rect(*table.bbox)
                    covered.append((page_number, rect))
                    placeholders.append(self._make_placeholder(index, page_number, rect, table))
        finally:
            doc.close()

        if not covered:
            return blocks, images, metadata

        kept = [b for b in blocks if not self._is_covered(b, covered)]
        removed = len(blocks) - len(kept)
        blocks = kept + placeholders

        images = images + self._charts_to_images(self.charts, len(images))

        logger.info(
            f"Exhibit pass: {len(self.tables)} tables, {len(self.charts)} charts; "
            f"{removed} text blocks folded into them"
        )
        return blocks, images, metadata

    def _build_table_detector(self, table_config) -> TableDetector:
        if table_config is None:
            return TableDetector()
        return TableDetector(
            min_rows=table_config.min_rows,
            min_columns=table_config.min_columns,
            min_numeric_ratio=table_config.min_numeric_ratio,
            column_gap=table_config.column_gap,
        )

    def _build_chart_detector(self, chart_config) -> ChartDetector:
        if chart_config is None:
            return ChartDetector()
        return ChartDetector(
            min_area_ratio=chart_config.min_area_ratio,
            max_text_density=chart_config.max_text_density,
            dpi=chart_config.dpi,
        )

    @staticmethod
    def _make_placeholder(
        index: int,
        page_number: int,
        rect: fitz.Rect,
        table: DetectedTable
    ) -> PlaceholderBlock:
        # The placeholder carries the table's text so word counts and any
        # text-level accounting still see the cells. Cells are joined by single
        # spaces on purpose: "digit + two spaces" is how the classifier
        # recognises an endnote, and a table must never be mistaken for one.
        flattened = " ".join(" ".join(table.cell_texts()).split())
        return PlaceholderBlock(
            table_index=index,
            text=flattened or f"Table {index + 1}",
            page=page_number,
            x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1,
            font_name="TablePlaceholder",
            font_size=10.0,
            flags=0,
        )

    @staticmethod
    def _is_covered(block: TextBlock, covered: List[Tuple[int, fitz.Rect]]) -> bool:
        """True when most of a block sits inside a detected table or chart."""
        block_rect = fitz.Rect(block.x0, block.y0, block.x1, block.y1)
        area = block_rect.get_area()
        for page_number, rect in covered:
            if page_number != block.page:
                continue
            padded = rect + (-2, -2, 2, 2)
            overlap = block_rect & padded
            if overlap.is_empty:
                continue
            if area <= 0:
                # Zero-height rules and the like: treat containment as coverage.
                if padded.contains(block_rect):
                    return True
                continue
            if overlap.get_area() / area > 0.6:
                return True
        return False

    def _charts_to_images(self, charts: List[DetectedChart], offset: int) -> List[ImageResource]:
        images = []
        for index, chart in enumerate(charts, start=1):
            filename = f"chart{offset + index:03d}.png"
            caption = self._chart_caption(chart)
            self._chart_captions[filename] = caption
            images.append(ImageResource(
                id=f"chart-page-{chart.page}-{index}",
                filename=filename,
                data=chart.data,
                format="png",
                width=chart.width,
                height=chart.height,
                page_num=chart.page,
                bbox=chart.bbox,
            ))
        return images

    @staticmethod
    def _chart_caption(chart: DetectedChart) -> str:
        """Describe a chart from the labels printed inside it."""
        words = [label for label in chart.labels if any(c.isalpha() for c in label)]
        if not words:
            return f"Chart from page {chart.page}"
        summary = " ".join(words)
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "..."
        return summary

    # ----------------------------------------------------------------- render

    def _render_chapters_with_images(self, chapters, images, config):
        rendered = super()._render_chapters_with_images(chapters, images, config)
        # Ship the table CSS with any chapter that needs it: the packaged
        # stylesheet is frozen and has no table rules.
        for chapter in rendered:
            if "<table" in chapter.content:
                chapter.content = TABLE_STYLESHEET + "\n" + chapter.content
        return rendered

    def _render_blocks_with_images(self, blocks, images, *args, **kwargs):
        """
        Render a run of blocks, splicing table markup in at its own position.

        The block list is cut at each placeholder and the inherited renderer is
        applied to the segments in between, so paragraph assembly, figure
        placement and endnote linking behave exactly as they do elsewhere —
        and table text never reaches the footnote linker, which would otherwise
        turn column keys like "(1)" into endnote references.
        """
        segments: List[List] = [[]]
        markers: List[int] = []

        for block in blocks:
            original = getattr(block, "original_block", None)
            if isinstance(original, PlaceholderBlock):
                markers.append(original.table_index)
                segments.append([])
            else:
                segments[-1].append(block)

        if not markers:
            return super()._render_blocks_with_images(blocks, images, *args, **kwargs)

        boundaries = [
            self._flow_key(b.original_block)
            for b in blocks
            if isinstance(getattr(b, "original_block", None), PlaceholderBlock)
        ]
        image_segments = self._partition_images(images, boundaries)

        parts: List[str] = []
        for index, segment in enumerate(segments):
            html = super()._render_blocks_with_images(
                segment, image_segments[index], *args, **kwargs
            )
            if html:
                parts.append(html)
            if index < len(markers):
                parts.append(self._render_table(markers[index]))

        return "\n".join(part for part in parts if part)

    def _render_table(self, table_index: int) -> str:
        if not 0 <= table_index < len(self.tables):
            return ""
        return self.renderer.render(self.tables[table_index])

    @staticmethod
    def _flow_key(block) -> Tuple[int, float, float]:
        return (block.page, block.y0, block.x0)

    def _partition_images(self, images, boundaries) -> List[List]:
        """Distribute images into the segments they fall between."""
        buckets: List[List] = [[] for _ in range(len(boundaries) + 1)]
        for image in images:
            key = (image.page_num, image.bbox[1] if image.bbox else 0.0,
                   image.bbox[0] if image.bbox else 0.0)
            index = 0
            while index < len(boundaries) and key > boundaries[index]:
                index += 1
            buckets[index].append(image)
        return buckets

    def _render_image_html(self, image: ImageResource) -> str:
        """Render charts as captioned figures; other images as before."""
        caption = self._chart_captions.get(image.filename)
        if caption is None:
            return super()._render_image_html(image)
        anchor_id = getattr(image, "anchor_id", image.id)
        return self.renderer.render_figure(
            filename=image.filename,
            anchor_id=anchor_id,
            alt_text=caption,
            caption=None,
        )

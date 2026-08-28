---
name: convert-pdf-to-epub
description: >
  Convert PDF books to EPUB format for e-readers. Use when user asks to convert PDF to EPUB,
  create an e-book from PDF, make PDF readable on Kindle/phone/tablet, or extract book content
  from PDF. Handles: chapter detection, image extraction with optimization, footnotes/endnotes
  with hyperlinks, reading order for multi-column layouts, and reconstruction of numeric tables
  and vector charts for technical books. Validates conversion quality automatically.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# PDF to EPUB Converter

Convert PDF documents to high-quality EPUB files with automatic chapter detection, image optimization, and footnote hyperlinking.

## Quick Start

```bash
# Run conversion (from skill directory)
python -m scripts.convert input.pdf output.epub

# Table-heavy or chart-heavy book (technical, actuarial, financial)
python -m scripts.convert input.pdf output.epub --strategy exhibit

# Validate result
python -m scripts.validate input.pdf output.epub
```

## Choosing a Strategy

| Strategy | Use for | What it adds |
|----------|---------|--------------|
| `simple` (default) | Prose: fiction, essays, narrative non-fiction | Paragraphs, headings, images, endnotes |
| `exhibit` | Technical books with data tables or charts | Reconstructs tables as real `<table>` markup and rasterizes vector charts as figures |

Check before converting: if `scripts.analyze` reports many pages and the PDF
is full of numeric grids, use `exhibit`. Rendering a table as loose paragraphs
destroys it, and the loss will not show up as missing text — the numbers are
all still there, just meaningless. See
[reference/tables-and-charts.md](reference/tables-and-charts.md).

## Workflow Overview

The conversion follows a 3-phase process with an optional 4th phase for adaptation:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  1. ANALYZE     │ ──► │  2. CONVERT     │ ──► │  3. VALIDATE    │
│  - PDF structure│     │  - Apply config │     │  - Check quality│
│  - Generate cfg │     │  - Build EPUB   │     │  - Report issues│
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────┐
                                              │  4. ADAPT       │
                                              │  (if needed)    │
                                              │  - Tune config  │
                                              │  - Modify code  │
                                              └─────────────────┘
```

### Phase 1: Analyze

Before converting, analyze the PDF to determine the best configuration:

```python
# Open PDF and examine structure
import fitz  # pymupdf
doc = fitz.open("input.pdf")

# Check for:
# 1. Number of pages
# 2. Presence of images
# 3. Multi-column layout (compare text block x-coordinates)
# 4. Footnotes/endnotes (numbers in margins or at page bottom)
# 5. Font sizes (for heading detection thresholds)
# 6. Tables and charts (numeric grids; pages whose drawings form a plot)
# 7. Page rotation — landscape pages are often portrait pages with /Rotate 90,
#    and their text coordinates come back unrotated
```

**Beware two traps when reading the analyzer's suggested config:**

- A book of landscape tables is often reported as *multi-column*. Those are
  table columns, not text columns; `xy_cut` will scramble the prose. Check a
  rendered page before accepting `multi_column`.
- `heading_detection.font_size_threshold` defaults to 1.2. If headings are only
  slightly larger than body text (12pt over 11pt = 1.09), that threshold
  detects nothing. Measure the actual font sizes.

**Generate initial config** based on analysis:
- Fiction book: Use default `y_sort` reading order
- Academic paper: Enable `xy_cut` for columns
- Magazine: Enable image optimization, use `xy_cut`

**Ask user to confirm** the proposed configuration before proceeding.

### Phase 2: Convert

Run the conversion with the generated config:

```python
from conversion.converter import Converter
from conversion.models import ConversionConfig

config = ConversionConfig(
    page_ranges=PageRanges(skip=[1, 2], content=(3, -3)),
    exclude_regions=ExcludeRegions(top=0.05, bottom=0.05),
    reading_order_strategy="y_sort",  # or "xy_cut" for columns
    image_optimization=ImageOptimizationConfig(enabled=True),
)

converter = Converter(strategy="simple")
result = converter.convert(pdf_path, epub_path, config)

# Check confidence
if result.reading_order_confidence < 0.7:
    print("Warning: Low confidence in reading order")
```

### Phase 3: Validate

Always validate the conversion result:

```python
from validation.completeness_checker import CompletenessChecker
from validation.order_checker import OrderChecker

# Check text completeness
completeness = CompletenessChecker().check(pdf_text, epub_text)
print(f"Completeness: {completeness.score:.1%}")  # Should be > 95%

# Check reading order
order = OrderChecker().check(pdf_chunks, epub_chunks)
print(f"Order score: {order.score:.1%}")  # Should be > 80%
```

**Quality gates:**
- Completeness < 95%: Text is being lost
- Order score < 80%: Reading order is wrong

### Phase 4: Adapt (if validation fails)

See [Decision Tree](#decision-tree-when-things-go-wrong) below.

---

## Decision Tree: When Things Go Wrong

```
Validation failed?
│
├─► Text loss > 5%?
│   ├─► Check exclude_regions (headers/footers being cut?)
│   │   → Try: exclude_regions.top: 0.03 (reduce from 0.05)
│   ├─► Check page_ranges (skipping too many pages?)
│   │   → Try: page_ranges.skip: [] (don't skip any)
│   └─► Still failing? → See reference/troubleshooting.md#text-loss
│
├─► Wrong reading order?
│   ├─► PDF has columns?
│   │   → Try: reading_order_strategy: "xy_cut"
│   ├─► Columns detected but wrong?
│   │   → Try: multi_column.threshold: 0.3 (more sensitive)
│   └─► Still failing? → See reference/troubleshooting.md#order
│
├─► Headings not detected?
│   ├─► Headings only slightly larger than body?
│   │   → Try: heading_detection.font_size_threshold: 1.1
│   └─► Custom font patterns?
│   │   → May need to modify structure_classifier.py (ADAPTABLE)
│
├─► Tables rendered as loose paragraphs, or a chart missing?
│   └─► Use --strategy exhibit
│       → See reference/tables-and-charts.md
│
├─► Table columns merged or split?
│   └─► Tune table_extraction.column_gap
│       → See reference/tables-and-charts.md#troubleshooting
│
├─► Footnotes not linking?
│   ├─► Non-standard format (not [1] or (1))?
│   │   → Add pattern to FootnoteDetector.PATTERNS
│   └─► See reference/troubleshooting.md#footnotes
│
└─► Other issue?
    └─► See reference/troubleshooting.md
```

---

## Three-Layer Architecture

The codebase is organized into three layers with different modification policies:

### Layer 1: FROZEN (Do Not Modify)

These files implement fixed specifications or deterministic algorithms:

| File | Reason |
|------|--------|
| `core/epub_builder.py` | EPUB3 spec is fixed |
| `core/text_segmenter.py` | Validation depends on identical chunking |
| `validation/*` | Metrics must be reproducible |

**Never modify these files** unless there's a fundamental bug.

### Layer 2: CONFIGURABLE (Try Config First)

Before changing code, try adjusting configuration:

```python
ConversionConfig:
├── page_ranges         # Which pages to process
├── exclude_regions     # Margins to ignore (headers/footers)
├── multi_column        # Column detection settings
├── table_extraction    # Table reconstruction (exhibit strategy)
├── chart_extraction    # Vector chart rasterization (exhibit strategy)
├── reading_order_strategy  # "y_sort" or "xy_cut"
├── heading_detection   # Font size thresholds
├── footnote_processing # Footnote patterns
├── image_optimization  # Compression settings
└── metadata           # Title, author, language
```

See [reference/config-tuning.md](reference/config-tuning.md) for all parameters.

### Layer 3: ADAPTABLE (Can Modify If Config Fails)

These files contain heuristics that may need tuning for specific PDFs:

| File | What You Can Modify |
|------|---------------------|
| `conversion/strategies/*` | Create new strategy subclass |
| `detectors/table_detector.py` | Table geometry heuristics |
| `detectors/table_renderer.py` | Table markup and CSS |
| `detectors/chart_detector.py` | Chart region detection, render DPI |
| `detectors/structure_classifier.py` | Heading detection heuristics |
| `detectors/reading_order/*` | Add custom sorter algorithm |
| `detectors/footnote_detector.py` | Add new footnote patterns |

See [reference/code-adaptation.md](reference/code-adaptation.md) for guidelines.

---

## Project Structure

```
<skill-directory>/
├── SKILL.md                     # This file
├── requirements.txt             # Python dependencies
├── core/                        # FROZEN: Core algorithms
│   ├── epub_builder.py          # EPUB3 file creation
│   ├── pdf_extractor.py         # PDF text/image extraction
│   ├── text_segmenter.py        # Deterministic chunking
│   └── image_optimizer.py       # Image compression
│
├── conversion/                  # Main conversion logic
│   ├── converter.py             # Orchestrator
│   ├── models.py                # Data classes & configs
│   ├── strategies/              # ADAPTABLE: Conversion strategies
│   │   ├── base_strategy.py     # Template method pattern
│   │   ├── simple_strategy.py   # Prose
│   │   └── exhibit_strategy.py  # Tables + charts
│   └── detectors/               # ADAPTABLE: Detection heuristics
│       ├── structure_classifier.py
│       ├── reading_order/
│       ├── footnote_detector.py
│       ├── endnote_formatter.py
│       ├── table_detector.py    # Table reconstruction
│       ├── table_renderer.py    # Table -> XHTML
│       └── chart_detector.py    # Vector charts -> PNG figures
│
├── validation/                  # FROZEN: Quality checking
│   ├── completeness_checker.py
│   └── order_checker.py
│
├── scripts/                     # CLI entry points
│   ├── analyze.py
│   ├── convert.py
│   └── validate.py
│
├── reference/                   # Documentation
│   ├── tables-and-charts.md
│   ├── workflow.md
│   ├── architecture.md
│   ├── troubleshooting.md
│   ├── config-tuning.md
│   └── code-adaptation.md
│
└── examples/                    # Example configurations
    ├── fiction-simple.json
    ├── academic-multicol.json
    └── magazine-images.json
```

---

## Example Configurations

### Fiction Book (simple layout)

```json
{
  "page_ranges": {"skip": [1, 2], "content": [3, -3]},
  "exclude_regions": {"top": 0.05, "bottom": 0.05},
  "reading_order_strategy": "y_sort",
  "heading_detection": {"font_size_threshold": 1.2}
}
```

### Academic Paper (2-column)

```json
{
  "page_ranges": {"skip": [1], "content": [2, -1]},
  "exclude_regions": {"top": 0.08, "bottom": 0.08},
  "reading_order_strategy": "xy_cut",
  "multi_column": {"enabled": true, "threshold": 0.4}
}
```

### Magazine (images + columns)

```json
{
  "reading_order_strategy": "xy_cut",
  "multi_column": {"enabled": true, "column_count": 2},
  "image_optimization": {
    "enabled": true,
    "max_width": 800,
    "jpeg_quality": 75
  }
}
```

---

## Reference Documentation

For detailed information, see:

- [Tables and Charts](reference/tables-and-charts.md) - The `exhibit` strategy
- [Workflow Details](reference/workflow.md) - Complete phase-by-phase guide
- [Architecture](reference/architecture.md) - Three-layer system explanation
- [Troubleshooting](reference/troubleshooting.md) - Common problems and solutions
- [Config Tuning](reference/config-tuning.md) - All configuration parameters
- [Code Adaptation](reference/code-adaptation.md) - When and how to modify code

---

## Common Commands

```bash
# Full conversion with validation (from skill directory)
python -m scripts.convert input.pdf output.epub && \
python -m scripts.validate input.pdf output.epub

# Analyze PDF structure
python -m scripts.analyze input.pdf

# Table-heavy book, with validation
python -m scripts.convert input.pdf output.epub --strategy exhibit && \
python -m scripts.validate input.pdf output.epub
```

---

## Quality Metrics

After conversion, always check:

| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Text completeness | > 98% | 95-98% | < 95% |
| Reading order | > 90% | 80-90% | < 80% |
| Confidence | > 0.8 | 0.6-0.8 | < 0.6 |

If any metric is in "Warning" or "Bad" range, follow the Decision Tree above.

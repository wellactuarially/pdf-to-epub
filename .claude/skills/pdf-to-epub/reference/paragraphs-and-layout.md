# Paragraphs and Layout

How text is segmented into paragraphs, and how wide tables are made to fit an
e-reader screen.

## Paragraph segmentation

A PyMuPDF text block is a *region of the page*, not a paragraph. On a prose
page it routinely covers the whole text area — every paragraph and every
heading on that page in one block. Emitting one unit per block gives the rest
of the pipeline nothing to work with: headings cannot be told apart from body
text, and a chapter renders as a single enormous `<p>`. On the sample book one
chapter came out as one 46,000-character paragraph.

`PDFExtractor._paragraph_groups` splits each block into paragraph-sized groups
of lines. A group ends at any of the four ways a document signals a new
paragraph:

| Signal | Why |
|---|---|
| A blank line | The most common separator in generated PDFs |
| A change of size or weight | A heading, or the note under a table |
| A step down the page larger than 1.5× the usual line pitch | Extra leading between paragraphs |
| An indented first line | Documents that indent instead of leaving a blank line |

The size-or-weight rule is what lifts headings out of the prose: a heading is
set differently, so it becomes its own group and the classifier can see it. On
the sample book this took detected headings from 282 to 693.

### The two consumers must agree

`get_structural_blocks` builds the EPUB; `_extract_page_content` produces the
reference text the conversion is validated against. Both run the same
segmentation. If one grouped by block and the other by paragraph, every
comparison near a boundary would read as a mismatch and the completeness score
would collapse for no real reason.

### Superscript note markers

Footnote handling keys on whitespace: a reference is spotted as a number after
punctuation and a space, a note's own start as a number followed by two
spaces. In the PDF that separation is purely typographic — the marker is a
small raised span carrying no spaces at all — so `_join_spans` restores it,
putting one space before a superscript numeral and two after.

The upstream code got the same effect by putting a space after *every* span.
That also works, but it leaves a stray space either side of every italic
phrase, which makes the page text disagree with the block text and turns
ordinary prose into fuzzy matches during validation.

## Wide tables

A 12-column claim development triangle is wider than a 6-inch e-ink screen at
any readable size. A horizontal scroll container is not a solution: e-ink
readers do not scroll sideways, so the right-hand columns are clipped off the
page edge and the exhibit is unreadable — the numbers are present in the file
but unreachable by the reader.

So a table too wide for the screen is **split into column groups**, the way a
wide table is handled in print. Each group repeats the row-label column and
carries a note reading "Part 2 of 2 — columns 8–12 of 12, row labels
repeated", so every number stays reachable and every row still lines up
against its label.

### How width is judged

Column count alone is a poor guide: nine columns of `1.070` fit easily where
six columns of `1,234,567,890` do not. Width is estimated in **character
units** — the widest value in each column plus two for padding — with cells
that span several columns ignored, since they wrap rather than forcing any one
column wider, and header text measured by its longest word for the same
reason.

The default budget of 105 units was measured, not guessed: rendered at the
table font size against a 600pt-wide screen, tables estimating 104 and 106
units fit exactly and one estimating 113 clipped.

### Configuration

```json
{
  "table_extraction": {
    "table_width_budget": 105,
    "max_columns_per_part": 12
  }
}
```

| Parameter | Effect | Try when |
|-----------|--------|----------|
| `table_width_budget` | Width, in character units, one part may occupy | Lower if columns still run off the page on your reader; raise for a wide screen or a small font |
| `max_columns_per_part` | Hard cap on columns per part | Lower to keep rows easy to follow across |

Set the budget very high to disable splitting entirely.

## Troubleshooting

**A chapter is still one huge paragraph.** The document separates paragraphs
by something other than the four signals above — most often it relies on a
short last line with no extra leading. Add that rule to `_starts_paragraph`
in `core/pdf_extractor.py`.

**Headings still read as body text.** They are set in the same size and weight
as the body, so nothing distinguishes them geometrically. Check
`heading_detection.font_size_threshold`, and see
[chapter-detection.md](chapter-detection.md).

**Paragraphs split mid-sentence.** The pitch factor is too tight for a
document with loose leading. Raise `PARAGRAPH_PITCH_FACTOR`.

**Endnote links stopped working.** Note markers are being run together with
the text around them. Check `_join_spans` recognises the marker: it must be a
short number in a span noticeably smaller than the line's dominant size.

**Tables still run off the page.** Lower `table_width_budget`. The default is
calibrated for a 600pt screen; a smaller device, or a reader with wide
margins, needs less.

# Chapter Detection

How chapters are found, why font size alone is not enough, and how to tune it.

## The two mechanisms

**Font-based headings.** `FontAnalyzer` and `StructureClassifier` score each
block on size, weight and surrounding whitespace. This is the primary
mechanism and works whenever a book prints its chapter titles in the body.

**Running headers.** `RunningHeaderDetector` reads the repeated line at the top
of each page and turns each run of consecutive pages sharing one into a
chapter. This runs first; when it succeeds, font-detected top-level headings
are demoted to `h2` so the chapter structure comes from one source.

## Why the second mechanism exists

Many technical books never print the chapter title in the body at all. The
only place "Chapter 5 - The Development Triangle" appears is the running head
— which header stripping correctly removes, because repeating it in the flow
is noise.

The result, with font detection alone, is a book with no detectable chapter
starts: every page lands in one enormous XHTML file, the table of contents is
empty or lists whatever stray large-font text happened to score highest, and
the reader has no way to navigate. On a 451-page sample book this produced a
single 2.4 MB chapter file and a four-entry contents list, two entries of
which were "Intro" and "* * * * *".

There is a second, quieter cost. A head naming one chapter repeats only across
that chapter's pages — far too small a share of a long book to trip the global
noise threshold — so it survives into the body and the chapter title is
reprinted at the top of every page's text. On the same book that was 382
interruptions. `PDFExtractor` now strips these separately, requiring only
three pages of support but matching only inside the header band, so a chapter
named in the body ("see Chapter 5 - The Development Triangle") is preserved.

## How a chapter is decided

1. Every page's header band (top 12% by default) is read in visual space, so
   rotated landscape pages work the same as upright ones.
2. Lines appearing on more than 80% of pages are the **book title** and are
   discarded: they divide nothing.
3. Among what remains, a line matching `Chapter|Part|Appendix|Annex|Section|
   Book` wins. Otherwise the most-repeated line is used, which is the running
   head in books that name chapters without the word "Chapter".
4. Consecutive pages sharing a head become one run. A page with no head — a
   chapter's opening page, a full-page figure — joins the previous run rather
   than breaking it.
5. Adjacent runs naming the **same division** are merged. Prose and exhibit
   pages of one chapter routinely word the head differently:

   | Prose pages | Exhibit pages |
   |---|---|
   | Chapter 11 - Frequency-Severity Techniques | Chapter 11 - Frequency-Severity Technique |
   | Chapter 12 – Case Outstanding Development Technique | Chapter 12 - Case Outstanding Development Technique |
   | Chapter 17 – Estimating Unpaid Unallocated Claim Adjustment Expenses | Chapter 17 - Unallocated Loss Adjustment Expenses |

   The identifier ("Chapter 11") decides; the first run's wording is kept,
   because that is the one the table of contents uses. Without this step the
   sample book produced 25 chapters instead of its actual 20.
6. If the result is fewer than `min_chapters` or more than `max_chapters`,
   **nothing is imposed**. A book that really does print its chapter titles is
   left entirely to the font-based detector.

## Configuration

```json
{
  "chapter_detection": {
    "use_running_headers": true,
    "header_zone": 0.12,
    "min_chapters": 2,
    "max_chapters": 80
  }
}
```

| Parameter | Effect | Try when |
|-----------|--------|----------|
| `use_running_headers` | Switches the mechanism off entirely | The book prints real chapter titles and the heads are misleading |
| `header_zone` | Fraction of page height read as the header band | Raise if heads sit low; lower if body text is being read as a head |
| `min_chapters` | Below this the detection is discarded | Raise to demand stronger evidence |
| `max_chapters` | Above this the detection is discarded | Lower if per-section heads are fragmenting the book |

## Troubleshooting

**One chapter per section, far too many.** The running head names sections
rather than chapters. Lower `max_chapters` so the detection is rejected, or
set `use_running_headers` to false.

**Chapters split in two at the same title.** The two halves word the head
differently in a way `DIVISION_PATTERN` does not capture. Add the document's
convention to that pattern in `detectors/running_header.py`.

**Chapter titles still repeated through the body.** The head sits below the
header band. Raise `header_zone`, or check that the head matches
`_CHAPTER_HEAD` in `core/pdf_extractor.py` — a head like "5.2 Triangles" with
no leading keyword needs that pattern extended.

**Everything in one file still.** Check the log for "Recovered N chapters from
running headers". If it says the runs were rejected, the count fell outside
`[min_chapters, max_chapters]`. If there is no line at all, no repeated header
was found and the book needs font-based detection instead — see
`heading_detection.font_size_threshold` in
[config-tuning.md](config-tuning.md), and note that the default of 1.2 detects
nothing in a book whose headings are 12pt over 11pt body.

**A chapter file is very large.** Chapters map one-to-one onto XHTML files. A
long chapter dense with tables can still reach a few hundred KB; that is the
chapter's real size, not a detection fault.

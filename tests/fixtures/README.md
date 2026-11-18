# Test fixtures

Original documents written for this project (no third-party content). They are byte-exact:
`.gitattributes` marks this folder `-text` and pre-commit skips it, so CRLF, trailing spaces and
blank lines survive. Use them in automated tests (via the `fixture_text` / `all_fixtures` pytest
fixtures in `tests/conftest.py`) **and** for manual checks: paste a file into the app or open it.

| File | What it exercises |
|---|---|
| `uk_article.txt` | Ukrainian prose, ~2.6k chars, several paragraphs, one very long sentence, «» „“ quotes, both apostrophes, numbers |
| `en_article.txt` | English prose, ~2.5k chars, indented bullet list, abbreviations (e.g., Dr.), quotes, a long URL |
| `long_paragraph.txt` | One ~2.2k-char paragraph without line breaks, including a >1500-char sentence that forces cuts between words |
| `cjk.txt` | Japanese and Chinese (sentence ends without spaces, 「」 closers) and Korean |
| `emoji.txt` | Astral characters: emoji, skin tones, ZWJ sequences, flags, keycaps, math letters, combining marks |
| `crlf_windows.txt` | CRLF line endings, blank CRLF lines, a lone CR and a lone LF |
| `mixed.txt` | Leading indentation, Markdown, tab indents, code block, URL/base64 without spaces, NBSP, trailing spaces, double blank lines, a trailing blank line |
| `field_notes.pdf` | Five text pages with headings, wrapped lines, first-line indents, and both compact and double line spacing |
| `scanned_field_notes.pdf` | One raster-only page with visible text and no extractable text layer |

When adding a fixture: keep it original, describe it here, and check that it is picked up by
`tests/core/test_fixtures.py` (all `*.txt` files are tested automatically).

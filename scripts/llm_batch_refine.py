#!/usr/bin/env python3
"""Create page drafts from a local VLM or the PDF OCR layer, then prepare chapters for agent review.

The script uses macOS PDFKit through a small Swift helper for page rendering
and OCR text extraction; it does not import a Python PDF library. Each page
draft is cached so either first pass can resume after interruption. Revised
chapter files are assembled only when every section is present.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

DEFAULT_API_URL = "http://127.0.0.1:1234/v1"
DEFAULT_PDF = "MasteEllioWav_single_OCR.pdf"
DEFAULT_OUTPUT_MD = "Mastering Elliot Wave/markdown/Mastering Elliot Wave.md"
DEFAULT_CACHE_DIR = ".cache_llm_text"
DEFAULT_AGENT_INPUT_DIR = ".cache_llm_text/agent_inputs"
DEFAULT_CHAPTER_MD_DIR = "Mastering Elliot Wave/markdown/chapters"
LOW_TEXT_REVIEW_THRESHOLD = 120

CHAPTER_TITLES = [
    "Elementary Discussions",
    "General Concepts",
    "Preliminary Analysis",
    "Intermediary Observations",
    "Central Considerations",
    "Post-Constructive Rules of Logic",
    "Conclusions",
    "Construction of Complex Polywaves",
    "Basic Neely Extensions",
    "Advanced Logic Rules",
    "Advanced Progress Label Application",
    "Advanced Neely Extensions",
    "Table of Contents",
]

VISION_MODEL_MARKERS = (
    "minicpm-v",
    "minicpm-o",
    "llava",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "internvl",
    "pixtral",
    "smolvlm",
    "idefics",
    "molmo",
    "phi-3.5-vision",
    "phi-4-multimodal",
    "gemma-3",
)

BOOK_SECTION_STARTS = [
    ("front-matter", "Front matter", 1),
    ("chapter-01", "Chapter 1: Elementary Discussions", 16),
    ("chapter-02", "Chapter 2: General Concepts", 28),
    ("chapter-03", "Chapter 3: Preliminary Analysis", 42),
    ("chapter-04", "Chapter 4: Intermediary Observations", 112),
    ("chapter-05", "Chapter 5: Central Considerations", 121),
    ("chapter-06", "Chapter 6: Post-Constructive Rules of Logic", 166),
    ("chapter-07", "Chapter 7: Conclusions", 172),
    (
        "chapter-08",
        "Chapter 8: Construction of Complex Polywaves, Multiwaves etc.",
        188,
    ),
    ("chapter-09", "Chapter 9: Basic Neely Extensions", 220),
    ("chapter-10", "Chapter 10: Advanced Logic Rules", 232),
    ("chapter-11", "Chapter 11: Advanced Progress Label Application", 244),
    ("chapter-12", "Chapter 12: Advanced Neely Extensions", 276),
    ("appendix", "Appendix: Forecast to the Year 2060", 323),
]

SYSTEM_PROMPT = """You are transcribing rendered page images from Glenn Neely's 'Mastering Elliott Wave' into accurate Markdown drafts.

Prioritize fidelity to the source page. Preserve meaning, technical claims, examples, equations, lists, and heading structure. Correct obvious OCR-like visual ambiguities only when the page itself supports the correction; never invent missing content.

### 1. Document Architecture & Heading Hierarchy
Use clear, semantic ATX headings:
- Book & Chapter Titles (#):
  - '# Chapter 1: Elementary Discussions' (Pages 16-27)
  - '# Chapter 2: General Concepts' (Pages 28-42)
  - '# Chapter 3: Preliminary Analysis' (Pages 43-112)
  - '# Chapter 4: Intermediary Observations'
  - '# Chapter 5: Central Considerations'
  - '# Chapter 6: Post-Constructive Rules of Logic'
  - '# Chapter 7: Conclusions'
  - '# Chapter 8: Construction of Complex Polywaves'
  - '# Chapter 9: Basic Neely Extensions'
  - '# Chapter 10: Advanced Logic Rules'
  - '# Chapter 11: Advanced Progress Label Application'
  - '# Chapter 12: Advanced Neely Extensions'
  * Note: The start of each chapter often has giant decorative letters that OCR garbles (e.g. 'fe E/emen ss/ons'). Replace these artifacts with the clean chapter header: '# Chapter N: [Title]'.
- Major Sections (##): E.g. '## What is the Elliott Wave Theory?', '## What is a "Wave"?', '## How Do You Categorize Waves?'.
- Subsections (###): E.g. '### Finances', '### Psychology', '### Classes', '### Degree', '### Structure Labels', '### Progress Labels'.
- Sub-subsections (####): For deeper nested items (e.g. '#### Closing Price Data', '#### Bar Charts').

### 2. Chapter Outline & Table of Contents Pages
- Each chapter begins with a Chapter Outline page (e.g. Page 16 for Chapter 1, Page 28 for Chapter 2) listing sections and page numbers.
- In raw OCR, these columns are often split into a list of titles followed by a clump of page numbers at the bottom.
- Format these pages as a clean, elegant Markdown outline:
  `## Chapter Outline`
  `- Section Title (Page-Number)`
  `- Sub-section Title (Page-Number)`
- Never output an isolated list of detached page numbers. Align each title with its page number.
- Correct obvious OCR corruptions in outline titles (e.g. 'Peri°diCPhemme\' ton' -> 'Natural, Non-Periodic Phenomenon', '4 Un^ue Analytical Tool' -> 'A Unique Analytical Tool', 'MSWPSSKVVFzaTheNeet%Eztensions' -> 'New Discoveries, The Neely Extensions').

### 3. Figure Rules (STRICT — ZERO HALLUCINATIONS)
- NEVER hallucinate or invent figure placeholders!
- IMPORTANT: Chapter 1 contains NO figures whatsoever. NEVER insert any '{{FIGURE: ...}}' placeholder in Chapter 1.
- For all other chapters (starting Chapter 2): ONLY insert a placeholder '{{FIGURE: Figure X-Y}}' when:
  1. The text explicitly refers to a figure by name (e.g. '(see Figure 2-1)', 'Figure 2-3b', etc.), OR
  2. A dedicated figure caption appears in the text.
- Placement: Place '{{FIGURE: Figure X-Y}}' on its own blank line between natural paragraphs.
- Sentence continuity: NEVER place a figure placeholder in the middle of an incomplete sentence! Keep the sentence whole, and put the placeholder before or after the paragraph.
- Never transcribe fragmented coordinate labels from inside charts (e.g. isolated 'Time Time', 'Price', floating numbers).

### 4. Technical Terminology & Notation Accuracy
Maintain exact Elliott Wave technical notation:
- Structure Labels: Always use ':3' and ':5' (colon followed by digit).
- Wave identifiers: 'm0', 'm1', 'm2', 'm3', 'm(-1)', 'mg0', 's1', 's2', 'c1', 'c2' (fix OCR typos like 'ml' -> 'm1', 'mO' -> 'm0', 'mgO' -> 'mg0').
- Core terms: 'Monowave', 'Polywave', 'Multiwave', 'Macrowave', 'Impulsion', 'Zigzag', 'Flat', 'Triangle' (fix 'Tigzags' -> 'Zigzags').
- De-hyphenate words broken across line wraps (e.g. 'mate- rialize' -> 'materialize').

### 5. Noise & Artifact Stripping
- Decorative dividers: Thick black underline bars under headings often OCR as blocks or symbols ('■■■■', 'IIIIII', 'MMMMMTTT...'). Do NOT transcribe these.
- Running headers and footers: Omit all page headers and footers (e.g. 'Elementary Discussions 1-3', '2-2 General Concepts', 'Table of Contents - 3').

### 6. Output Formatting
- Output ONLY the page's Markdown content.
- Do NOT wrap your entire answer in ```markdown ... ``` code fences.
- Absolutely NO conversational filler, greetings, or post-hoc notes. Start directly with the content.
- Do NOT output page markers; the script adds them to cached drafts for source traceability."""


def select_model(models: List[str], vision_required: bool = False) -> str:
    if vision_required:
        for model in models:
            if any(marker in model.lower() for marker in VISION_MODEL_MARKERS):
                return model
        raise ValueError(
            "No known vision-capable model is available. Load a vision-tuned model "
            "(such as MiniCPM-V, Qwen-VL, or LLaVA) or pass --model explicitly."
        )

    for preferred in ["Huihui", "Qwen", "MiniCPM"]:
        for model in models:
            if preferred.lower() in model.lower():
                return model
    if models:
        return models[0]
    raise ValueError("The local model server did not advertise any models.")


def get_available_model(api_base: str, vision_required: bool = False) -> str:
    url = f"{api_base}/models"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", []) if m.get("id")]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not fetch models from {url}: {exc}") from exc
    return select_model(models, vision_required=vision_required)


def build_book_sections(total_pages: int) -> List[dict]:
    if total_pages < 1:
        raise ValueError("The PDF must contain at least one page.")

    starts = [page for _, _, page in BOOK_SECTION_STARTS]
    if starts[0] != 1 or any(
        current >= following for current, following in zip(starts, starts[1:])
    ):
        raise ValueError("Book section start pages must be strictly increasing from page 1.")
    if starts[-1] > total_pages:
        raise ValueError(
            f"The PDF has {total_pages} pages, fewer than the appendix start page {starts[-1]}."
        )

    sections = []
    for index, (section_id, title, start_page) in enumerate(BOOK_SECTION_STARTS):
        end_page = (
            BOOK_SECTION_STARTS[index + 1][2] - 1
            if index + 1 < len(BOOK_SECTION_STARTS)
            else total_pages
        )
        sections.append(
            {
                "id": section_id,
                "title": title,
                "start_page": start_page,
                "end_page": end_page,
            }
        )
    return sections


def format_page_ranges(page_numbers: List[int]) -> str:
    pages = sorted(set(page_numbers))
    if not pages or pages[0] < 1:
        raise ValueError("At least one positive page number is required.")

    ranges = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def write_agent_inputs(
    sections: List[dict], page_cache_dir: Path, output_dir: Path
) -> List[Path]:
    missing_pages = [
        page
        for section in sections
        for page in range(section["start_page"], section["end_page"] + 1)
        if not (page_cache_dir / f"page_{page:03d}.md").is_file()
    ]
    if missing_pages:
        sample = ", ".join(str(page) for page in missing_pages[:10])
        more = " ..." if len(missing_pages) > 10 else ""
        raise FileNotFoundError(
            f"Missing VLM page drafts for pages {sample}{more}; finish the first pass before preparing agent inputs."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for section in sections:
        source_pages = [
            (page_cache_dir / f"page_{page:03d}.md").read_text(encoding="utf-8").strip()
            for page in range(section["start_page"], section["end_page"] + 1)
        ]
        output_path = output_dir / f"{section['id']}.source.md"
        output_path.write_text("\n\n".join(source_pages), encoding="utf-8")
        paths.append(output_path)
    return paths


def assemble_markdown(
    sections: List[dict], chapter_dir: Path, output_path: Path
) -> None:
    section_texts = []
    missing_files = []
    for section in sections:
        chapter_path = chapter_dir / f"{section['id']}.md"
        if not chapter_path.is_file():
            missing_files.append(chapter_path)
            continue
        content = chapter_path.read_text(encoding="utf-8").strip()
        if not content:
            missing_files.append(chapter_path)
            continue
        content = re.sub(
            r"<!--\s*(?:Source\s+)?Page\s+\d+\s*-->\s*",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip()
        content = content.replace("](../figures/", "](figures/")
        section_texts.append(content)

    if missing_files:
        missing = ", ".join(str(path) for path in missing_files)
        raise FileNotFoundError(f"Missing or empty revised section files: {missing}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n\n".join(section_texts).rstrip() + "\n", encoding="utf-8")


def is_noise_divider(line: str) -> bool:
    """Detect OCR scan artifacts of thick decorative bars under headings."""
    s = re.sub(r"\s+", "", line)
    if len(s) < 5:
        return False
    noise_chars = sum(1 for c in s if c in "■IMTil1|-_=~^/\\•*™S")
    return (noise_chars / len(s)) >= 0.8


def is_running_header_or_footer(line: str) -> bool:
    """Detect running headers/footers like 'Elementary Discussions 1-3' or '2-2 General Concepts'."""
    ch_pat = "|".join(re.escape(t) for t in CHAPTER_TITLES)
    if re.match(rf"^\s*(?:{ch_pat})\s*[-–—]?\s*\d*(?:\s*[-–—]\s*\d+)?\s*$", line, re.IGNORECASE):
        return True
    if re.match(rf"^\s*\d*(?:\s*[-–—]\s*\d+)?\s*[-–—]?\s*(?:{ch_pat})\s*$", line, re.IGNORECASE):
        return True
    if re.match(rf"^\s*\d+[a-zA-Z]?\s+(?:{ch_pat})\s*$", line, re.IGNORECASE):
        return True
    return False


def clean_llm_output(text: str) -> str:
    """Strip any conversational chatter, horizontal rules, or trailing note disclaimers."""
    text = re.sub(
        r"^(?:(?:I\'ll|Here is|Sure|I will|Below is|Certainly)[^\n]*\n+)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Strip wrapping code fences (e.g. ```markdown ... ```)
    text = re.sub(r"^\s*```(?:markdown)?\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n\s*```\s*$", "", text)

    text = re.sub(r"^\s*---+\s*\n", "", text)
    text = re.sub(
        r"\n+\s*(?:---+\s*\n+)?(?:_?Note:?|Disclaimer:|Here ends)[^\n]*.*$",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Post-filter: remove any hallucinated Figure 1-X (Chapter 1 has NO figures)
    text = re.sub(r"\{\{FIGURE:\s*Figure\s*1-\d+\w*\}\}\n*", "", text, flags=re.IGNORECASE)

    # Filter any residual noise lines that slipped through
    filtered_lines = []
    for line in text.splitlines():
        if is_running_header_or_footer(line.strip()) or is_noise_divider(line.strip()):
            continue
        filtered_lines.append(line)
    text = "\n".join(filtered_lines)

    return text.strip()


def parse_page_range(spec: str, max_page: int) -> List[int]:
    selected = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            start_num = int(start) if start else 1
            end_num = int(end) if end else max_page
            for p in range(start_num, end_num + 1):
                if 1 <= p <= max_page:
                    selected.add(p)
        else:
            p = int(token)
            if 1 <= p <= max_page:
                selected.add(p)
    return sorted(selected)


def call_vlm(
    api_base: str,
    model: str,
    page_img_b64: str,
    page_num: int,
    timeout: int = 180,
) -> str:
    url = f"{api_base}/chat/completions"
    user_instruction = f"""Transcribe this book page (Page {page_num}) of 'Mastering Elliott Wave' into pristine, publication-grade Markdown.
Follow all formatting, heading hierarchy, outline, and figure placeholder rules strictly.
Output ONLY the transcribed Markdown."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_instruction},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{page_img_b64}"
                        },
                    },
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"VLM request failed for page {page_num}: {exc}") from exc

    try:
        raw = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"VLM response for page {page_num} did not contain message content."
        ) from exc
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(f"VLM returned empty content for page {page_num}.")
    return clean_llm_output(raw)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create page drafts from a local VLM or PDF OCR text, then prepare agent review."
    )
    parser.add_argument(
        "--mode",
        choices=["vlm", "ocr", "prepare-agents", "assemble"],
        default="vlm",
        help="Run the VLM or native OCR first pass, prepare chapter inputs, or assemble revised chapters.",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path(DEFAULT_PDF),
        help=f"Input PDF (default: {DEFAULT_PDF})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT_MD),
        help=f"Final Markdown output for assemble mode (default: {DEFAULT_OUTPUT_MD})",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_URL, help="OpenAI-compatible VLM API base URL.")
    parser.add_argument("--model", default=None, help="Vision model ID (auto-detected when omitted).")
    parser.add_argument("--dpi", type=float, default=150.0, help="PDF rendering resolution (default: 150).")
    parser.add_argument("--pages", help="Pages to process in first-pass mode, e.g. 1-10,28-35.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(DEFAULT_CACHE_DIR),
        help=f"Page draft and render cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--agent-input-dir",
        type=Path,
        default=Path(DEFAULT_AGENT_INPUT_DIR),
        help=f"Chapter input directory for prepare-agents mode (default: {DEFAULT_AGENT_INPUT_DIR})",
    )
    parser.add_argument(
        "--chapter-dir",
        type=Path,
        default=Path(DEFAULT_CHAPTER_MD_DIR),
        help=f"Revised chapter Markdown directory (default: {DEFAULT_CHAPTER_MD_DIR})",
    )
    parser.add_argument(
        "--renderer",
        type=Path,
        default=Path(__file__).with_name("render_pdf_pages.swift"),
        help="Native PDFKit renderer script.",
    )
    return parser


def get_pdf_page_count(pdf_path: Path, renderer_path: Optional[Path] = None) -> int:
    renderer = renderer_path or Path(__file__).with_name("render_pdf_pages.swift")
    result = subprocess.run(
        ["swift", str(renderer), str(pdf_path), "--count"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        page_count = int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Native renderer returned an invalid page count: {result.stdout!r}"
        ) from exc
    if page_count < 1:
        raise RuntimeError(f"PDF has an invalid page count: {page_count}.")
    return page_count


def render_pdf_pages(
    pdf_path: Path,
    output_dir: Path,
    dpi: float,
    page_numbers: List[int],
    renderer_path: Optional[Path] = None,
) -> None:
    if not page_numbers:
        return
    if dpi <= 0:
        raise ValueError("DPI must be a positive number.")
    renderer = renderer_path or Path(__file__).with_name("render_pdf_pages.swift")
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "swift",
            str(renderer),
            str(pdf_path),
            str(output_dir),
            str(dpi),
            format_page_ranges(page_numbers),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )


def process_ocr_pages(
    pdf_path: Path,
    page_numbers: List[int],
    cache_dir: Path,
    renderer_path: Optional[Path] = None,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    text_dir = cache_dir / "ocr_text"
    uncached_pages = [
        page
        for page in page_numbers
        if not (cache_dir / f"page_{page:03d}.md").is_file()
    ]
    if uncached_pages:
        renderer = renderer_path or Path(__file__).with_name("render_pdf_pages.swift")
        text_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "swift",
                str(renderer),
                str(pdf_path),
                "--extract-text",
                str(text_dir),
                format_page_ranges(uncached_pages),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )

    page_texts = {}
    pages_without_text = []
    for page_num in uncached_pages:
        text_path = text_dir / f"page_{page_num:03d}.txt"
        if not text_path.is_file():
            raise RuntimeError(f"Native PDFKit extractor did not create {text_path}.")
        page_texts[page_num] = text_path.read_text(encoding="utf-8").strip()
        if not page_texts[page_num]:
            pages_without_text.append(page_num)

    pages_for_image_review = [
        page_num
        for page_num in uncached_pages
        if not page_texts[page_num]
        or len(page_texts[page_num]) < LOW_TEXT_REVIEW_THRESHOLD
    ]
    if pages_for_image_review:
        render_pdf_pages(
            pdf_path,
            cache_dir / "rendered",
            300.0,
            pages_for_image_review,
            renderer_path,
        )

    for index, page_num in enumerate(uncached_pages, start=1):
        text = page_texts[page_num]
        if not text:
            image_path = cache_dir / "rendered" / f"page_{page_num:03d}.jpg"
            text = (
                f"[No OCR text was available for source page {page_num}. "
                f"Inspect {image_path.as_posix()} if possible; do not infer missing content.]"
            )
        elif page_num in pages_for_image_review:
            image_path = cache_dir / "rendered" / f"page_{page_num:03d}.jpg"
            text = (
                f"{text}\n\n[Low-text OCR on source page {page_num}; "
                f"inspect {image_path.as_posix()} for figures or omitted text.]"
            )
        cache_file = cache_dir / f"page_{page_num:03d}.md"
        cache_file.write_text(
            f"<!-- Page {page_num} -->\n\n{text}\n",
            encoding="utf-8",
        )
        status = (
            "flagged for agent image review"
            if page_num in pages_for_image_review
            else "cached OCR text"
        )
        print(f"[{index}/{len(uncached_pages)}] Page {page_num} {status}.")


def validate_vlm_transcription(text: str, page_num: int) -> str:
    cleaned = clean_llm_output(text)
    if not cleaned:
        raise RuntimeError(f"VLM returned empty Markdown for page {page_num}.")
    if re.search(
        r"page content (?:is )?not provided|"
        r"no image (?:was )?(?:attached|provided)|"
        r"image (?:was )?not (?:attached|provided)|"
        r"(?:i do not|i don't) see (?:any )?image|"
        r"please provide (?:the )?(?:rendered )?page image",
        cleaned,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError(
            f"VLM did not process the page image for page {page_num}; "
            "verify that the selected model supports vision input."
        )
    return cleaned


def process_vlm_pages(
    pdf_path: Path,
    page_numbers: List[int],
    cache_dir: Path,
    api_base: str,
    model: str,
    dpi: float,
    renderer_path: Optional[Path] = None,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    image_dir = cache_dir / "rendered"
    uncached_pages = [
        page
        for page in page_numbers
        if not (cache_dir / f"page_{page:03d}.md").is_file()
    ]
    render_pdf_pages(pdf_path, image_dir, dpi, uncached_pages, renderer_path)

    for index, page_num in enumerate(uncached_pages, start=1):
        image_path = image_dir / f"page_{page_num:03d}.jpg"
        if not image_path.is_file():
            raise RuntimeError(f"Native renderer did not create {image_path}.")
        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        print(
            f"[{index}/{len(uncached_pages)}] Transcribing page {page_num} ... ",
            end="",
            flush=True,
        )
        start_time = time.time()
        cleaned = validate_vlm_transcription(
            call_vlm(api_base, model, image_base64, page_num),
            page_num,
        )
        cache_file = cache_dir / f"page_{page_num:03d}.md"
        cache_file.write_text(
            f"<!-- Page {page_num} -->\n\n{cleaned}\n",
            encoding="utf-8",
        )
        print(f"done ({time.time() - start_time:.1f}s)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if not args.pdf.is_file():
        parser.error(f"Input PDF '{args.pdf}' does not exist.")

    try:
        total_pages = get_pdf_page_count(args.pdf, args.renderer)
        if args.mode == "ocr":
            target_pages = (
                parse_page_range(args.pages, total_pages)
                if args.pages
                else list(range(1, total_pages + 1))
            )
            if not target_pages:
                raise ValueError("The page selection is empty.")
            print(f"Extracting searchable PDF text with native PDFKit from {args.pdf}.")
            process_ocr_pages(
                args.pdf,
                target_pages,
                args.cache_dir,
                args.renderer,
            )
            print(f"OCR page drafts are cached in {args.cache_dir}; run prepare-agents after completion.")
            return 0

        if args.mode == "vlm":
            target_pages = (
                parse_page_range(args.pages, total_pages)
                if args.pages
                else list(range(1, total_pages + 1))
            )
            if not target_pages:
                raise ValueError("The page selection is empty.")
            model = args.model or get_available_model(args.api_base, vision_required=True)
            print(f"PDF: {args.pdf} ({total_pages} pages)")
            print(f"VLM: {model} at {args.api_base}")
            print(f"Pages: {format_page_ranges(target_pages)}")
            process_vlm_pages(
                args.pdf,
                target_pages,
                args.cache_dir,
                args.api_base,
                model,
                args.dpi,
                args.renderer,
            )
            print(f"Page drafts are cached in {args.cache_dir}; run prepare-agents after completion.")
            return 0

        sections = build_book_sections(total_pages)
        if args.mode == "prepare-agents":
            paths = write_agent_inputs(sections, args.cache_dir, args.agent_input_dir)
            print(f"Prepared {len(paths)} section inputs in {args.agent_input_dir}.")
            return 0

        assemble_markdown(sections, args.chapter_dir, args.output)
        print(f"Assembled revised Markdown at {args.output}.")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

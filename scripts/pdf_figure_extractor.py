#!/usr/bin/env python3
"""
pdfFigureExtractor - Semantic Figure & Chart Extractor for Technical PDFs.

Instead of dumping unsemantic, fragmented XObject image slices (which lose axes,
labels, and context), this tool semantically locates figures by caption (e.g. "Figure 2-1",
"Figure 3-21a"), computes the exact visual bounding box containing the diagram, axes,
and annotations, renders it at high DPI, auto-trims whitespace, and saves it.

Can be used in two modes:
1. Single Figure mode:
   ./venv/bin/python scripts/pdf_figure_extractor.py --pdf MasteEllioWav_OCR.pdf --figure "Figure 2-1" --output figures/Figure_2-1.png

2. Markdown Backfill mode:
   Scan a Markdown file for figure placeholders (e.g. `{{FIGURE: Figure 2-1}}` or `![Figure 2-1](...)`),
   extract all referenced figures, and replace placeholders with the final image links.
   ./venv/bin/python scripts/pdf_figure_extractor.py --pdf MasteEllioWav_OCR.pdf --markdown input.md --output output.md --figure-dir figures/
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote


FIGURE_ID = r"Figure\s+\d+(?:-\d+)?[A-Za-z]?"
FIGURE_ANCHOR_PATTERNS = [
    (
        "bracket",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)(?:>\s*)?\*?\[(?P<figure_id>{FIGURE_ID})"
            r"(?P<caption>[^\]\r\n]*)\]\*?(?P<trailing>[^\r\n]*)$",
            re.IGNORECASE,
        ),
        0,
    ),
    (
        "template",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)\{{\{{FIGURE:\s*(?P<figure_id>{FIGURE_ID})\s*\}}\}}$",
            re.IGNORECASE,
        ),
        0,
    ),
    (
        "comment",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)<!--\s*FIGURE:\s*(?P<figure_id>{FIGURE_ID})\s*-->$",
            re.IGNORECASE,
        ),
        0,
    ),
    (
        "image",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)(?:>\s*)?!\[(?P<figure_id>{FIGURE_ID})\]"
            r"\((?P<path>[^)\r\n]+)\)(?P<trailing>[^\r\n]*)$",
            re.IGNORECASE,
        ),
        0,
    ),
    (
        "heading",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)#{{1,6}}\s+(?P<figure_id>{FIGURE_ID})"
            r"(?P<caption>[^\r\n]*)$",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        "bold",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)(?:>\s*)?\*\*(?P<figure_id>{FIGURE_ID})"
            r"(?:[.:])?\*\*(?P<caption>[^\r\n]*)$",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "bare",
        re.compile(
            rf"(?m)^(?P<indent>[ \t]*)(?P<figure_id>{FIGURE_ID})\s*$",
            re.IGNORECASE,
        ),
        3,
    ),
]


def _normalize_figure_id(figure_id: str) -> str:
    return " ".join(figure_id.split()).casefold()


def _figure_filename(figure_id: str) -> str:
    normalized = re.sub(r"\s+", "_", figure_id.strip())
    return f"{normalized}.png"


def _read_coordinate_manifest(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read coordinate manifest {manifest_path}: {exc}") from exc

    figures = manifest.get("figures") if isinstance(manifest, dict) else None
    if not isinstance(figures, list):
        raise ValueError("Coordinate manifest must contain a 'figures' list.")

    entries = {}
    for entry in figures:
        if not isinstance(entry, dict) or not isinstance(entry.get("figure_id"), str):
            raise ValueError("Each coordinate entry must have a string 'figure_id'.")
        key = _normalize_figure_id(entry["figure_id"])
        if key in entries:
            raise ValueError(f"Duplicate coordinate entry for {entry['figure_id']}.")
        entries[key] = entry
    return entries


def _figure_anchors(content: str) -> list:
    candidates = []
    for kind, pattern, priority in FIGURE_ANCHOR_PATTERNS:
        for match in pattern.finditer(content):
            candidates.append(
                {
                    "kind": kind,
                    "match": match,
                    "priority": priority,
                    "key": _normalize_figure_id(match.group("figure_id")),
                }
            )

    best_priority = {}
    for candidate in candidates:
        key = candidate["key"]
        best_priority[key] = min(
            candidate["priority"], best_priority.get(key, candidate["priority"])
        )
    return sorted(
        [
            candidate
            for candidate in candidates
            if candidate["priority"] == best_priority[candidate["key"]]
        ],
        key=lambda candidate: candidate["match"].start(),
    )


def _caption_from_placeholder(match: re.Match) -> str:
    inside = (match.groupdict().get("caption") or "").strip()
    trailing = (match.groupdict().get("trailing") or "").strip()
    inside = re.sub(r"^(?::|[-\u2013\u2014])\s*", "", inside)
    trailing = re.sub(r"^(?:[-\u2013\u2014:]\s*)", "", trailing).strip()
    return "; ".join(part for part in (inside, trailing) if part)


def _replace_figure_placeholders(
    content: str, links: dict[str, str]
) -> tuple[str, int]:
    anchors = _figure_anchors(content)
    replacements = 0
    for anchor in reversed(anchors):
        match = anchor["match"]
        figure_id = " ".join(match.group("figure_id").split())
        link = links.get(anchor["key"])
        if not link:
            continue

        indent = match.group("indent")
        image_link = f"{indent}![{figure_id}]({link})"
        kind = anchor["kind"]
        if kind == "heading":
            replacement = f"{match.group(0)}\n\n{image_link}"
        elif kind == "bold":
            replacement = f"{image_link}\n\n{match.group(0)}"
        elif kind == "image":
            replacement = image_link + (match.groupdict().get("trailing") or "")
        else:
            caption = _caption_from_placeholder(match)
            replacement = image_link
            if caption:
                replacement += f"\n\n{indent}*{figure_id} — {caption}*"

        content = (
            content[: match.start()]
            + replacement
            + content[match.end() :]
        )
        replacements += 1
    return content, replacements


def backfill_coordinate_markdown(
    pdf_path: Path,
    markdown_in: Path,
    markdown_out: Path,
    figure_dir: Path,
    manifest_path: Path,
    dpi: float,
) -> None:
    content = markdown_in.read_text(encoding="utf-8")
    anchors = _figure_anchors(content)
    if not anchors:
        raise ValueError(f"No standalone figure anchors found in {markdown_in}.")

    manifest = _read_coordinate_manifest(manifest_path)
    figure_ids = {
        anchor["key"]: " ".join(anchor["match"].group("figure_id").split())
        for anchor in anchors
    }
    missing = [
        figure_id
        for key, figure_id in figure_ids.items()
        if key not in manifest
    ]
    unresolved = [
        figure_ids[key]
        for key in figure_ids
        if key in manifest
        and (
            manifest[key].get("status") == "unresolved"
            or not isinstance(manifest[key].get("bbox"), dict)
        )
    ]
    if missing or unresolved:
        details = []
        if missing:
            details.append("missing coordinates: " + ", ".join(missing))
        if unresolved:
            details.append("unresolved coordinates: " + ", ".join(unresolved))
        raise ValueError(
            "Markdown was not changed; " + "; ".join(details) + "."
        )

    if dpi <= 0:
        raise ValueError("DPI must be a positive number.")

    renderer = Path(__file__).with_name("crop_pdf_figures.swift")
    try:
        subprocess.run(
            [
                "swift",
                str(renderer),
                str(pdf_path),
                str(manifest_path),
                str(figure_dir),
                str(dpi),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or str(exc)
        raise RuntimeError(f"Native PDFKit cropper failed: {detail}") from exc

    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    links = {}
    for key, figure_id in figure_ids.items():
        image_path = figure_dir / _figure_filename(figure_id)
        if not image_path.is_file():
            raise RuntimeError(f"Figure renderer did not create {image_path}.")
        relative_path = os.path.relpath(
            image_path.resolve(), markdown_out.parent.resolve()
        ).replace(os.sep, "/")
        links[key] = quote(relative_path, safe="/._-")

    updated, replacements = _replace_figure_placeholders(content, links)
    if replacements != len(anchors):
        raise RuntimeError(
            f"Replaced {replacements} of {len(anchors)} figure anchors."
        )

    temporary_path = markdown_out.with_name(markdown_out.name + ".tmp")
    try:
        temporary_path.write_text(updated, encoding="utf-8")
        temporary_path.replace(markdown_out)
    finally:
        temporary_path.unlink(missing_ok=True)


def auto_trim_whitespace(img, padding: int = 12):
    """Trim excess white borders around the diagram and add a clean padding."""
    from PIL import Image, ImageChops

    # Convert to grayscale for thresholding
    gray = img.convert("L")
    # Invert so black content is foreground
    bg = Image.new("L", gray.size, 255)
    diff = ImageChops.difference(gray, bg)
    # Threshold slight scan background noise (pixel values > 20)
    diff = diff.point(lambda p: 255 if p > 20 else 0)
    bbox = diff.getbbox()
    if bbox:
        w, h = img.size
        x0 = max(0, bbox[0] - padding)
        y0 = max(0, bbox[1] - padding)
        x1 = min(w, bbox[2] + padding)
        y1 = min(h, bbox[3] + padding)
        return img.crop((x0, y0, x1, y1))
    return img


class PdfFigureExtractor:
    def __init__(self, pdf_path: Path, dpi: float = 250.0):
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError(
                "Automatic caption extraction requires pypdfium2; use --coordinates "
                "for the native PDFKit crop workflow."
            ) from exc

        self.pdf_path = pdf_path
        self.dpi = dpi
        self.scale = dpi / 72.0
        self.doc = pdfium.PdfDocument(str(pdf_path))
        self.page_count = len(self.doc)

    def locate_figure_caption(
        self, figure_name: str
    ) -> Optional[dict]:
        """Search PDF pages for a standalone figure caption (excluding inline references)."""
        target_norm = re.sub(r"[\s\-_]+", "", figure_name).lower()

        best = None
        for p_idx in range(self.page_count):
            page = self.doc[p_idx]
            tp = page.get_textpage()

            search = tp.search(figure_name)
            while True:
                res = search.get_next()
                if not res:
                    break
                c_idx, count = res
                boxes = [tp.get_charbox(c_idx + i) for i in range(count)]
                bx0 = min(b[0] for b in boxes)
                by0 = min(b[1] for b in boxes)
                bx1 = max(b[2] for b in boxes)
                by1 = max(b[3] for b in boxes)

                # Check preceding context
                start_before = max(0, c_idx - 15)
                context_before = "".join(
                    tp.get_text_range(start_before, c_idx - start_before)
                ).strip().lower()

                # An in-sentence reference usually has '(' or 'see' or 'like' right before it
                is_reference = (
                    "see" in context_before
                    or "like" in context_before
                    or "(" in context_before
                )

                score = 10 if not is_reference else 1
                candidate = {
                    "p_idx": p_idx,
                    "page": page,
                    "tp": tp,
                    "bx0": bx0,
                    "by0": by0,
                    "bx1": bx1,
                    "by1": by1,
                    "score": score,
                }
                if best is None or score > best["score"]:
                    best = candidate
                if score == 10:
                    return best

        return best

    def compute_figure_bounds(
        self, caption_info: dict
    ) -> Tuple[float, float, float, float]:
        """Compute the PDF coordinate box [x0, y0, x1, y1] for the entire diagram."""
        page = caption_info["page"]
        tp = caption_info["tp"]
        cap_bx0 = caption_info["bx0"]
        cap_by0 = caption_info["by0"]
        cap_by1 = caption_info["by1"]

        page_w = page.get_width()
        page_h = page.get_height()
        mid_x = page_w / 2.0

        is_left = cap_bx0 < mid_x
        col_x0 = 42.0 if is_left else mid_x + 10.0
        col_x1 = mid_x - 10.0 if is_left else page_w - 42.0

        # Scan text rects on the page below cap_by0 to find the next section / body text
        num_rects = tp.count_rects(0, tp.count_chars())
        bottom_y = 65.0  # default bottom page margin
        next_anchor_y = 0.0

        for i in range(num_rects):
            r = tp.get_rect(i)
            rx0, ry0, rx1, ry1 = r[0], r[1], r[2], r[3]
            rw = rx1 - rx0
            rh = ry1 - ry0

            in_col = (col_x0 - 15 <= rx0) and (rx1 <= col_x1 + 15)
            # Find the top of the closest text line below the diagram
            if in_col and ry1 < cap_by0 - 18:
                txt = tp.get_text_bounded(*r).strip()
                is_fig = bool(re.match(r"^Figure\s+\d+", txt, re.IGNORECASE))
                is_heading = (rh >= 8.0 and rw > 50.0) or (rx0 <= col_x0 + 50.0 and rw > 70.0)
                is_body_para = rw > 150.0
                if (is_fig or is_heading or is_body_para) and ry1 > next_anchor_y:
                    next_anchor_y = ry1

        if next_anchor_y > 0.0:
            bottom_y = next_anchor_y + 8.0  # leave 8 pt buffer above the body line

        top_y = min(page_h - 15.0, cap_by1 + 14.0)
        return (col_x0, bottom_y, col_x1, top_y)

    def extract_figure(
        self, figure_name: str, output_path: Path
    ) -> bool:
        """Locate, render, crop, and save a figure by its name."""
        cap_info = self.locate_figure_caption(figure_name)
        if not cap_info:
            print(f"Warning: Could not locate caption for '{figure_name}' in PDF.")
            return False

        page = cap_info["page"]
        p_idx = cap_info["p_idx"]
        page_h = page.get_height()

        x0, y0, x1, y1 = self.compute_figure_bounds(cap_info)

        # Render page
        bitmap = page.render(scale=self.scale)
        full_img = bitmap.to_pil()

        # PDF coords (y=0 bottom) to PIL pixel coords (y=0 top)
        crop_box = (
            int(x0 * self.scale),
            int((page_h - y1) * self.scale),
            int(x1 * self.scale),
            int((page_h - y0) * self.scale),
        )

        cropped = full_img.crop(crop_box)
        final_img = auto_trim_whitespace(cropped)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        final_img.save(str(output_path), format="PNG")
        print(
            f"Extracted '{figure_name}' (PDF p.{p_idx+1}) -> {output_path} ({final_img.width}x{final_img.height} px)"
        )
        return True

    def backfill_markdown(
        self, markdown_in: Path, markdown_out: Path, figure_dir: Path
    ):
        """Find all figure placeholders in markdown, extract figures, and replace links."""
        content = markdown_in.read_text(encoding="utf-8")
        figure_dir.mkdir(parents=True, exist_ok=True)

        # Match patterns like:
        # {{FIGURE: Figure 2-1}}
        # <!-- FIGURE: Figure 2-1 -->
        # Standalone lines: Figure 2-1, ### Figure 2-1, **Figure 2-1**
        # ![Figure 2-1](placeholder)
        patterns = [
            r"\{\{FIGURE:\s*(Figure\s*[\d\w\-\.]+)\}\}",
            r"<!--\s*FIGURE:\s*(Figure\s*[\d\w\-\.]+)\s*-->",
            r"!\[([^\]]*Figure\s*[\d\w\-\.]+)\]\(figure:([^\)]+)\)",
            r"!\[(Figure\s*[\d\w\-\.]+)\]\((?:placeholder|figure_placeholder|[^\)]*)\)",
            r"(?m)^\s*#{1,6}\s*(Figure\s*[\d\w\-\.]+)\s*$",
            r"(?m)^\s*\*\*(Figure\s*[\d\w\-\.]+)\*\*\s*$",
            r"(?m)^\s*(Figure\s*[\d\w\-\.]+)\s*$",
        ]

        # Extract all figure references
        figures_to_extract = set()
        for pat in patterns:
            for match in re.finditer(pat, content, re.IGNORECASE):
                fig_label = match.group(1).strip()
                figures_to_extract.add(fig_label)

        print(f"Found {len(figures_to_extract)} unique figure placeholders in {markdown_in}:")
        for f in sorted(figures_to_extract):
            print(f"  - {f}")

        # Extract figures
        fig_map = {}
        for fig_name in sorted(figures_to_extract):
            # Normalize filename, e.g. "Figure 2-1" -> "Figure_2-1.png"
            clean_name = re.sub(r"\s+", "_", fig_name)
            out_img = figure_dir / f"{clean_name}.png"
            if not out_img.exists():
                ok = self.extract_figure(fig_name, out_img)
            else:
                ok = True
                print(f"Using cached figure: {out_img}")

            if ok:
                # Relative path from markdown to figure_dir
                fig_map[fig_name] = str(out_img)

        # Replace in content
        def get_img_link(fig_str: str) -> Optional[str]:
            norm_str = re.sub(r"\s+", " ", fig_str.strip().lower())
            for k in fig_map:
                if re.sub(r"\s+", " ", k.lower()) == norm_str:
                    return f"![{k}]({fig_map[k]})"
            return None

        updated = content
        # Replace {{FIGURE: ...}} and <!-- FIGURE: ... -->
        def replace_tag(match):
            name = match.group(1).strip()
            link = get_img_link(name)
            return link if link else match.group(0)

        updated = re.sub(r"\{\{FIGURE:\s*(Figure\s*[\d\w\-\.]+)\}\}", replace_tag, updated, flags=re.IGNORECASE)
        updated = re.sub(r"<!--\s*FIGURE:\s*(Figure\s*[\d\w\-\.]+)\s*-->", replace_tag, updated, flags=re.IGNORECASE)
        updated = re.sub(r"!\[(Figure\s*[\d\w\-\.]+)\]\([^\)]*\)", replace_tag, updated, flags=re.IGNORECASE)

        # Replace standalone lines
        def replace_standalone(match):
            name = match.group(1).strip()
            link = get_img_link(name)
            return link if link else match.group(0)

        updated = re.sub(r"(?m)^\s*#{1,6}\s*(Figure\s*[\d\w\-\.]+)\s*$", replace_standalone, updated, flags=re.IGNORECASE)
        updated = re.sub(r"(?m)^\s*\*\*(Figure\s*[\d\w\-\.]+)\*\*\s*$", replace_standalone, updated, flags=re.IGNORECASE)
        updated = re.sub(r"(?m)^\s*(Figure\s*[\d\w\-\.]+)\s*$", replace_standalone, updated, flags=re.IGNORECASE)

        markdown_out.write_text(updated, encoding="utf-8")
        print(f"\nSuccessfully backfilled markdown to: {markdown_out}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract complete semantic figures/charts from PDF and backfill Markdown"
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("MasteEllioWav_OCR.pdf"),
        help="Input PDF file (e.g. MasteEllioWav_OCR.pdf)",
    )
    parser.add_argument(
        "--figure",
        type=str,
        default=None,
        help="Single figure name to extract (e.g. 'Figure 2-1')",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output image path for single figure extraction",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Input markdown file containing {{FIGURE: ...}} placeholders",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Output markdown file with backfilled real image paths",
    )
    parser.add_argument(
        "--coordinates",
        type=Path,
        default=None,
        help="Agent-reviewed JSON manifest of figure pages and normalized crop boxes",
    )
    parser.add_argument(
        "--figure-dir",
        type=Path,
        default=Path("figures"),
        help="Directory to save extracted figure images (default: figures/)",
    )
    parser.add_argument(
        "--dpi",
        type=float,
        default=250.0,
        help="Rendering resolution DPI (default: 250.0)",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"Error: PDF '{args.pdf}' not found.", file=sys.stderr)
        sys.exit(1)

    if args.coordinates:
        if not args.markdown:
            parser.error("--coordinates requires --markdown.")
        if not args.coordinates.is_file():
            parser.error(f"Coordinate manifest '{args.coordinates}' not found.")
        markdown_out = args.markdown_out or args.markdown
        try:
            backfill_coordinate_markdown(
                args.pdf,
                args.markdown,
                markdown_out,
                args.figure_dir,
                args.coordinates,
                args.dpi,
            )
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Backfilled coordinate-reviewed figures to: {markdown_out}")
    elif args.figure:
        extractor = PdfFigureExtractor(args.pdf, dpi=args.dpi)
        out = args.output or (args.figure_dir / f"{re.sub(r'\s+', '_', args.figure)}.png")
        extractor.extract_figure(args.figure, out)
    elif args.markdown:
        extractor = PdfFigureExtractor(args.pdf, dpi=args.dpi)
        md_out = args.markdown_out or args.markdown
        extractor.backfill_markdown(args.markdown, md_out, args.figure_dir)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

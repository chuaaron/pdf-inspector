import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pdf_figure_extractor.py"
FIXTURE_PDF = ROOT / "tests" / "fixtures" / "vector_outlined_text_with_caption.pdf"
SPEC = importlib.util.spec_from_file_location("pdf_figure_extractor", SCRIPT)
pdf_figure_extractor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pdf_figure_extractor)


class CoordinateFigureExtractionTests(unittest.TestCase):
    def test_backfills_bracket_heading_and_caption_figure_markers(self):
        source = (
            "### Figure 1a: Historical chart\n\n"
            "> [Figure 1-1]\n\n"
            "**Figure 1-2.** A trendline caption.\n"
        )
        links = {
            "figure 1a": "figures/Figure_1a.png",
            "figure 1-1": "figures/Figure_1-1.png",
            "figure 1-2": "figures/Figure_1-2.png",
        }

        updated, replacements = pdf_figure_extractor._replace_figure_placeholders(
            source, links
        )

        self.assertEqual(replacements, 3)
        self.assertIn("### Figure 1a: Historical chart\n\n![Figure 1a](figures/Figure_1a.png)", updated)
        self.assertIn("![Figure 1-1](figures/Figure_1-1.png)", updated)
        self.assertIn("![Figure 1-2](figures/Figure_1-2.png)\n\n**Figure 1-2.** A trendline caption.", updated)

    def test_extracts_agent_box_and_backfills_relative_image_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            markdown_path = root / "book.md"
            markdown_path.write_text(
                "# Sample\n\n*[Figure 1-1: Diagram caption.]*\n",
                encoding="utf-8",
            )
            figure_dir = root / "figures"
            manifest_path = root / "coordinates.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "figures": [
                            {
                                "figure_id": "Figure 1-1",
                                "page": 1,
                                "bbox": {
                                    "left": 0.05,
                                    "top": 0.05,
                                    "right": 0.95,
                                    "bottom": 0.95,
                                },
                                "confidence": "high",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--pdf",
                    str(FIXTURE_PDF),
                    "--markdown",
                    str(markdown_path),
                    "--figure-dir",
                    str(figure_dir),
                    "--coordinates",
                    str(manifest_path),
                    "--dpi",
                    "72",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            updated = markdown_path.read_text(encoding="utf-8")
            self.assertIn("![Figure 1-1](figures/Figure_1-1.png)", updated)
            self.assertIn("*Figure 1-1 — Diagram caption.*", updated)
            self.assertNotIn("*[Figure 1-1", updated)

            image_path = figure_dir / "Figure_1-1.png"
            self.assertTrue(image_path.is_file())
            self.assertTrue(image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()

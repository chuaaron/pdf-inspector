import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "llm_batch_refine.py"
RENDERER_PATH = SCRIPT_PATH.with_name("render_pdf_pages.swift")
SPEC = importlib.util.spec_from_file_location("llm_batch_refine", SCRIPT_PATH)
llm_batch_refine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm_batch_refine)


class BookSectionsTests(unittest.TestCase):
    def test_book_sections_cover_front_matter_chapters_and_appendix(self):
        sections = llm_batch_refine.build_book_sections(335)

        self.assertEqual(
            [
                (section["id"], section["start_page"], section["end_page"])
                for section in sections
            ],
            [
                ("front-matter", 1, 15),
                ("chapter-01", 16, 27),
                ("chapter-02", 28, 41),
                ("chapter-03", 42, 111),
                ("chapter-04", 112, 120),
                ("chapter-05", 121, 165),
                ("chapter-06", 166, 171),
                ("chapter-07", 172, 187),
                ("chapter-08", 188, 219),
                ("chapter-09", 220, 231),
                ("chapter-10", 232, 243),
                ("chapter-11", 244, 275),
                ("chapter-12", 276, 322),
                ("appendix", 323, 335),
            ],
        )

    def test_visual_mode_selects_minicpm_before_text_models(self):
        model = llm_batch_refine.select_model(
            [
                "Huihui-Ornith-1.5-35B-A3B-abliterated-oQ5e-fp16-mtp",
                "MiniCPM-V-2_6-8B-MLX-4bit",
            ],
            vision_required=True,
        )

        self.assertEqual(model, "MiniCPM-V-2_6-8B-MLX-4bit")

    def test_visual_mode_errors_when_no_vision_model_is_available(self):
        with self.assertRaisesRegex(ValueError, "vision-capable"):
            llm_batch_refine.select_model(
                ["Qwen3-4B-4bit", "MiniCPM5-2B-heretic-abliterated-MLX-8bit"],
                vision_required=True,
            )

    def test_format_page_ranges_coalesces_adjacent_pages(self):
        self.assertEqual(
            llm_batch_refine.format_page_ranges([8, 2, 1, 5, 4, 3]),
            "1-5,8",
        )

    def test_parser_defaults_to_vlm_and_exposes_review_workflow_modes(self):
        args = llm_batch_refine.build_argument_parser().parse_args([])

        self.assertEqual(args.mode, "vlm")
        self.assertEqual(args.pdf, Path(llm_batch_refine.DEFAULT_PDF))
        self.assertIn("ocr", llm_batch_refine.build_argument_parser()._option_string_actions["--mode"].choices)

    def test_pdf_page_count_uses_native_renderer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_pdf = Path(temp_dir) / "fixture.pdf"
            fixture_pdf.write_bytes(b"fixture")
            renderer = Path(temp_dir) / "render_pdf_pages.swift"
            renderer.write_text("")

            with mock.patch.object(
                llm_batch_refine.subprocess,
                "run",
                return_value=mock.Mock(stdout="335\n"),
            ) as run:
                page_count = llm_batch_refine.get_pdf_page_count(
                    fixture_pdf, renderer_path=renderer
                )

        self.assertEqual(page_count, 335)
        self.assertIn("--count", run.call_args.args[0])


class AgentInputTests(unittest.TestCase):
    def test_sparse_ocr_pages_are_rendered_for_figure_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"

            def create_sparse_text_file(command, **_kwargs):
                output_dir = Path(command[4])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "page_001.txt").write_text(
                    "Figure 1-1 A short figure caption.",
                    encoding="utf-8",
                )
                return mock.Mock(stdout="", stderr="")

            with (
                mock.patch.object(
                    llm_batch_refine.subprocess,
                    "run",
                    side_effect=create_sparse_text_file,
                ),
                mock.patch.object(llm_batch_refine, "render_pdf_pages") as render,
            ):
                llm_batch_refine.process_ocr_pages(
                    Path("book.pdf"), [1], cache_dir, renderer_path=RENDERER_PATH
                )

            draft = (cache_dir / "page_001.md").read_text(encoding="utf-8")
            self.assertIn("Low-text OCR", draft)
            render.assert_called_once_with(
                Path("book.pdf"),
                cache_dir / "rendered",
                300.0,
                [1],
                RENDERER_PATH,
            )

    def test_empty_ocr_pages_are_flagged_and_rendered_for_agent_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"

            def create_empty_text_file(command, **_kwargs):
                output_dir = Path(command[4])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "page_001.txt").write_text("", encoding="utf-8")
                return mock.Mock(stdout="", stderr="")

            with (
                mock.patch.object(
                    llm_batch_refine.subprocess,
                    "run",
                    side_effect=create_empty_text_file,
                ),
                mock.patch.object(llm_batch_refine, "render_pdf_pages") as render,
            ):
                llm_batch_refine.process_ocr_pages(
                    Path("book.pdf"), [1], cache_dir, renderer_path=RENDERER_PATH
                )

            draft = (cache_dir / "page_001.md").read_text(encoding="utf-8")
            self.assertIn("No OCR text was available", draft)
            render.assert_called_once_with(
                Path("book.pdf"),
                cache_dir / "rendered",
                300.0,
                [1],
                RENDERER_PATH,
            )

    def test_native_ocr_mode_caches_text_per_page(self):
        fixture_pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/thermo-freon12.pdf"
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"

            llm_batch_refine.process_ocr_pages(
                fixture_pdf, [1], cache_dir, renderer_path=RENDERER_PATH
            )

            draft = (cache_dir / "page_001.md").read_text(encoding="utf-8")
            self.assertTrue(draft.startswith("<!-- Page 1 -->"))
            self.assertGreater(len(draft.strip()), len("<!-- Page 1 -->"))

    def test_refusal_to_read_page_is_not_cached_as_a_transcription(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "cache"
            image_dir = cache_dir / "rendered"

            def create_rendered_image(_pdf, output_dir, _dpi, _pages, _renderer):
                output_dir.mkdir(parents=True)
                (output_dir / "page_001.jpg").write_bytes(b"jpeg")

            with (
                mock.patch.object(
                    llm_batch_refine,
                    "render_pdf_pages",
                    side_effect=create_rendered_image,
                ),
                mock.patch.object(
                    llm_batch_refine,
                    "call_vlm",
                    return_value="No image attached.",
                ),
                self.assertRaisesRegex(RuntimeError, "did not process the page image"),
            ):
                llm_batch_refine.process_vlm_pages(
                    Path("book.pdf"), [1], cache_dir, "http://localhost/v1",
                    "MiniCPM", 150,
                )

            self.assertFalse((cache_dir / "page_001.md").exists())
            self.assertTrue((image_dir / "page_001.jpg").exists())

    def test_agent_inputs_group_all_page_drafts_by_section(self):
        sections = [
            {
                "id": "chapter-01",
                "title": "Chapter 1",
                "start_page": 1,
                "end_page": 2,
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page_cache_dir = root / "pages"
            page_cache_dir.mkdir()
            (page_cache_dir / "page_001.md").write_text("<!-- Page 1 -->\n\nFirst")
            (page_cache_dir / "page_002.md").write_text("<!-- Page 2 -->\n\nSecond")

            paths = llm_batch_refine.write_agent_inputs(
                sections, page_cache_dir, root / "agent-inputs"
            )

            self.assertEqual([path.name for path in paths], ["chapter-01.source.md"])
            self.assertEqual(
                paths[0].read_text(),
                "<!-- Page 1 -->\n\nFirst\n\n<!-- Page 2 -->\n\nSecond",
            )

    def test_assembly_follows_book_order_and_removes_page_markers(self):
        sections = [
            {"id": "chapter-01", "title": "Chapter 1", "start_page": 1, "end_page": 1},
            {"id": "chapter-02", "title": "Chapter 2", "start_page": 2, "end_page": 2},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chapter_dir = root / "chapters"
            chapter_dir.mkdir()
            (chapter_dir / "chapter-01.md").write_text(
                "# Chapter 1\n\n<!-- Page 1 -->\n\nFirst\n\n![Figure 1-1](../figures/Figure_1-1.png)"
            )
            (chapter_dir / "chapter-02.md").write_text(
                "# Chapter 2\n\n<!-- Page 2 -->\n\nSecond"
            )
            output_path = root / "book.md"

            llm_batch_refine.assemble_markdown(sections, chapter_dir, output_path)

            output = output_path.read_text()
            self.assertLess(output.index("# Chapter 1"), output.index("# Chapter 2"))
            self.assertNotIn("<!-- Page", output)
            self.assertIn("First\n\n![Figure 1-1](figures/Figure_1-1.png)\n\n# Chapter 2", output)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swift"), "requires macOS PDFKit")
class NativePdfRendererTests(unittest.TestCase):
    def test_extracts_text_with_pdfkit_without_python_pdf_libraries(self):
        fixture_pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/thermo-freon12.pdf"
        with tempfile.TemporaryDirectory() as output_dir:
            subprocess.run(
                [
                    "swift",
                    str(RENDERER_PATH),
                    str(fixture_pdf),
                    "--extract-text",
                    output_dir,
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            text = (Path(output_dir) / "page_001.txt").read_text(encoding="utf-8")

        self.assertTrue(text.strip())

    def test_reports_page_count_without_a_python_pdf_library(self):
        fixture_pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/thermo-freon12.pdf"
        result = subprocess.run(
            ["swift", str(RENDERER_PATH), str(fixture_pdf), "--count"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.stdout.strip(), "3")

    def test_renders_a_pdf_page_without_a_python_pdf_library(self):
            fixture_pdf = Path(__file__).resolve().parents[2] / "tests/fixtures/thermo-freon12.pdf"
            with tempfile.TemporaryDirectory() as output_dir:
                subprocess.run(
                    [
                        "swift",
                        str(RENDERER_PATH),
                        str(fixture_pdf),
                        output_dir,
                        "150",
                        "1",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                rendered_page = Path(output_dir) / "page_001.jpg"
                self.assertTrue(rendered_page.is_file())
                self.assertTrue(rendered_page.read_bytes().startswith(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()

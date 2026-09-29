from __future__ import annotations

import shutil
import subprocess
import tempfile
from io import BytesIO
from math import ceil, floor
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from pypdf import PdfReader


class ScreenshotError(RuntimeError):
    """A source page or webpage could not be rendered to PNG."""


def render_pdf_page(pdf: Path, output: Path, *, page: int, dpi: int = 150) -> Path:
    if output.suffix.lower() != ".png":
        raise ScreenshotError("PDF screenshot output must end in .png.")
    if not pdf.is_file():
        raise ScreenshotError(f"PDF does not exist: {pdf}")
    if not 36 <= dpi <= 600:
        raise ScreenshotError("DPI must be between 36 and 600.")
    try:
        page_count = len(PdfReader(pdf).pages)
    except Exception as exc:
        raise ScreenshotError(f"Cannot read PDF {pdf}: {exc}") from exc
    if not 1 <= page <= page_count:
        raise ScreenshotError(f"Page must be between 1 and {page_count}.")
    executable = shutil.which("pdftoppm")
    if executable is None:
        raise ScreenshotError("Poppler pdftoppm is missing from the active environment.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf2html-pdf-") as temporary:
        prefix = Path(temporary) / "page"
        result = subprocess.run(
            [
                executable,
                "-f", str(page),
                "-l", str(page),
                "-r", str(dpi),
                "-png",
                "-singlefile",
                str(pdf),
                str(prefix),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        image = prefix.with_suffix(".png")
        if result.returncode != 0 or not image.is_file():
            raise ScreenshotError(result.stderr.strip() or "pdftoppm did not create a PNG.")
        shutil.move(str(image), output)
    return output


def render_webpage(
    url: str,
    output: Path,
    *,
    width: int = 1440,
    height: int = 900,
    full_page: bool = False,
    selector: str | None = None,
    wait_for: str | None = None,
) -> Path:
    if output.suffix.lower() != ".png":
        raise ScreenshotError("Webpage screenshot output must end in .png.")
    if urlsplit(url).scheme not in {"http", "https", "file"}:
        raise ScreenshotError("Webpage URL must use http, https, or file.")
    if not 320 <= width <= 7680 or not 200 <= height <= 4320:
        raise ScreenshotError("Viewport width or height is out of range.")
    from playwright.sync_api import Error, sync_playwright

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height}, device_scale_factor=1
                )
                response = page.goto(url, wait_until="load", timeout=30000)
                if response is not None and response.status >= 400:
                    raise ScreenshotError(f"Webpage returned HTTP {response.status}: {url}")
                if wait_for:
                    page.locator(wait_for).first.wait_for(state="visible", timeout=30000)
                page.evaluate("document.fonts.ready")
                if selector:
                    locator = page.locator(selector).first
                    locator.wait_for(state="visible", timeout=30000)
                    bounds = locator.evaluate(
                        "element => { const r = element.getBoundingClientRect(); "
                        "return { x: r.x + scrollX, y: r.y + scrollY, "
                        "width: r.width, height: r.height }; }"
                    )
                    if bounds["width"] <= 0 or bounds["height"] <= 0:
                        raise ScreenshotError(f"Selected element has no visible area: {selector}")
                    screenshot = page.screenshot(full_page=True, timeout=30000)
                    with Image.open(BytesIO(screenshot)) as image:
                        image.crop(
                            (
                                floor(bounds["x"]), floor(bounds["y"]),
                                ceil(bounds["x"] + bounds["width"]),
                                ceil(bounds["y"] + bounds["height"]),
                            )
                        ).save(output)
                else:
                    page.screenshot(path=str(output), full_page=full_page, timeout=30000)
            finally:
                browser.close()
    except Error as exc:
        raise ScreenshotError(
            f"Chromium screenshot failed: {exc}. Run `pixi run install-browser` "
            "if the Linux browser is missing."
        ) from exc
    return output

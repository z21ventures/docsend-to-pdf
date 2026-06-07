#!/usr/bin/env python3
"""
docsend_scraper.py — capture every slide from a DocSend link as clean PNGs.

Strategy:
  1. Dismiss the Dropbox CCPA cookie-consent iframe (cross-origin; handled via frame).
  2. Fill the email gate that DocSend shows before any slide renders.
  3. Intercept each slide image directly from network responses (full CDN resolution).
  4. Fall back to element / viewport screenshots if interception misses a slide.

Usage:
    python3 docsend_scraper.py <url> [options]

Setup (one-time):
    pip3 install playwright pillow
    playwright install chromium

Examples:
    python3 docsend_scraper.py https://docsend.com/view/abc123xyz
    python3 docsend_scraper.py https://docsend.com/view/abc123xyz --email me@example.com
    python3 docsend_scraper.py https://docsend.com/view/abc123xyz --output deck --scale 2
    python3 docsend_scraper.py https://docsend.com/view/abc123xyz --show-browser
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import mimetypes
import re
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Page,
    Frame,
    Response,
    TimeoutError as PWTimeout,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

COUNTER_RE = re.compile(r"(\d+)\s*[/|]\s*(\d+)|(\d+)\s+of\s+(\d+)", re.I)


def _parse_counter(text: str) -> Optional[tuple[int, int]]:
    m = COUNTER_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    return int(m.group(3)), int(m.group(4))


def safe_filename(name: str, default: str = "deck", max_len: int = 80) -> str:
    """Turn an arbitrary string into a safe filename *base* (no extension).

    Strips any user-typed .pdf, drops characters unsafe for filenames, and
    collapses whitespace to underscores. Falls back to ``default`` if nothing
    usable remains.
    """
    if not name:
        return default
    name = re.sub(r"\.pdf$", "", name.strip(), flags=re.I)
    # Keep word chars (incl. unicode letters), spaces, dots and hyphens
    name = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    name = name.strip("._-")[:max_len].strip("._-")
    return name or default


def _clean_title(title: str) -> str:
    """Strip DocSend boilerplate from a page title; '' if it's generic."""
    title = re.sub(r"\s*[-|–—]\s*DocSend.*$", "", title, flags=re.I)
    title = re.sub(r"^DocSend\s*[-|–—:]\s*", "", title, flags=re.I)
    title = title.strip()
    if title.lower() in {"", "docsend", "view", "document", "untitled"}:
        return ""
    return title


# ── Scraper ────────────────────────────────────────────────────────────────────

class DocSendScraper:
    def __init__(
        self,
        output_dir: str = "slides",
        email: Optional[str] = None,
        headless: bool = True,
        scale: float = 2.0,
        extra_wait_ms: int = 0,
        show_browser: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.email = email
        self.headless = headless and not show_browser
        self.scale = scale
        self.extra_wait_ms = extra_wait_ms

        self.page: Optional[Page] = None
        # Deck title detected from the page after auth (used for default naming)
        self.deck_title: Optional[str] = None
        # Slide images intercepted from network: page_num -> bytes
        self._intercepted: dict[int, bytes] = {}
        # Accumulate all large image responses so we can match them to slides
        self._all_large_images: list[tuple[str, bytes]] = []

    # ── Public ─────────────────────────────────────────────────────────────────

    async def scrape(self, url: str) -> int:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output → {self.output_dir.resolve()}/")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                ],
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=self.scale,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            self.page = await ctx.new_page()

            # Intercept every image response ≥ 20 KB
            self.page.on("response", self._on_response)

            try:
                count = await self._run(url)
            finally:
                await browser.close()

        return count

    # ── Network interception ───────────────────────────────────────────────────

    async def _on_response(self, resp: Response) -> None:
        if resp.request.resource_type != "image":
            return
        ct = resp.headers.get("content-type", "")
        if not ct.startswith("image/"):
            return
        try:
            body = await resp.body()
        except Exception:
            return
        if len(body) < 20_000:          # skip tiny icons
            return
        self._all_large_images.append((resp.url, body))

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run(self, url: str) -> int:
        page = self.page
        print(f"Opening  {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(3_000)

        # Step 1 — dismiss the Dropbox cookie-consent banner (cross-origin iframe)
        await self._dismiss_cookie_banner()

        # Step 2 — fill the email gate (required before slides render)
        await self._handle_email_gate()

        # Wait for the presentation to finish loading
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2_000)

        # Detect the deck title for context-aware default naming
        self.deck_title = await self._detect_title()
        if self.deck_title:
            print(f"Deck title: {self.deck_title}")

        # Detect total from the toolbar counter
        total = await self._detect_total()
        if total:
            print(f"Detected {total} slides")
        else:
            print("Slide count unknown — will navigate until stuck")

        # Navigate and capture
        slide_num   = 1
        prev_info: Optional[tuple[int, int]] = None
        prev_hash: Optional[str] = None

        while True:
            await self._wait_for_load()

            info = await self._get_counter()

            # Stuck-detection via counter — only after the second slide attempt
            # (prev_info[0] must equal the NAVIGATED-TO slide, not the pre-auth 1/12)
            if prev_info is not None and info is not None and info[0] == prev_info[0] and slide_num > 1:
                print(f"\nCounter did not advance (still {info[0]}/{info[1]}). Done.")
                break

            label = f"{slide_num}" + (f"/{total}" if total else "")
            print(f"  Slide {label} …", end="", flush=True)

            # Mark how many images we had before triggering this slide
            imgs_before = len(self._all_large_images)

            # Give network a moment for the slide image to arrive
            await page.wait_for_timeout(max(self.extra_wait_ms, 800))

            img_bytes = await self._capture_slide(slide_num, imgs_before)

            # Pixel-hash stuck-detection (catches end-of-deck without counter)
            cur_hash = hashlib.md5(img_bytes).hexdigest()
            if prev_hash is not None and cur_hash == prev_hash:
                out = self.output_dir / f"slide_{slide_num:03d}.png"
                out.unlink(missing_ok=True)
                slide_num -= 1
                print(f"\nDuplicate frame — end of deck. Done.")
                break

            print(" ✓")
            prev_info = info
            prev_hash = cur_hash

            if total and slide_num >= total:
                print(f"\nAll {total} slides captured. Done.")
                break

            at_end = await self._navigate_next()
            if at_end:
                print("\nNext button disabled — end of deck. Done.")
                break

            slide_num += 1
            if slide_num > 500:
                print("\nSafety cap (500). Done.")
                break

        return slide_num

    # ── Cookie-consent (Dropbox CCPA iframe) ──────────────────────────────────

    async def _dismiss_cookie_banner(self) -> None:
        """Click 'Accept all' or 'Decline' inside the Dropbox CCPA iframe."""
        page = self.page

        # First try: interact with the iframe frame object
        for frame in page.frames:
            if "dropbox.com" in frame.url and "ccpa" in frame.url:
                for sel in [
                    "button:has-text('Accept all')",
                    "button:has-text('Accept All')",
                    "button:has-text('Decline')",
                    "button[class*='accept']",
                    "[aria-label='Close']",
                ]:
                    try:
                        btn = frame.locator(sel).first
                        if await btn.count() > 0 and await btn.is_visible(timeout=1_500):
                            await btn.click(timeout=3_000)
                            await page.wait_for_timeout(800)
                            print("Cookie banner dismissed")
                            return
                    except Exception:
                        continue

        # Second try: just remove the banner iframe from the DOM silently
        removed = await page.evaluate("""() => {
            const iframe = document.querySelector('iframe[class*="ccpa"], iframe[id*="ccpa"]');
            if (iframe) { iframe.remove(); return true; }
            return false;
        }""")
        if removed:
            print("Cookie banner removed (DOM)")
            await page.wait_for_timeout(500)

    # ── Email gate ─────────────────────────────────────────────────────────────

    async def _handle_email_gate(self) -> None:
        """
        DocSend's email gate:
          - input#link_auth_form_email  (exact id from DOM inspection)
          - form#new_link_auth_form submits as PATCH → page reloads into the deck
        """
        page = self.page

        # The exact input id found by DOM inspection
        GATE_INPUT = "#link_auth_form_email"

        try:
            await page.wait_for_selector(GATE_INPUT, timeout=6_000, state="visible")
        except PWTimeout:
            return  # No gate on this link

        email = self.email
        if not email:
            email = input(
                "\nThis DocSend link requires an email address to view.\n"
                "Enter your email: "
            ).strip()

        inp = page.locator(GATE_INPUT)
        await inp.click()
        await inp.fill(email)
        await page.wait_for_timeout(400)

        # The visible Continue button (type=submit inside #new_link_auth_form)
        # DOM inspection showed: button.dig-Button--primary text="Continue", visible=True
        submitted = False
        for sel in [
            "#new_link_auth_form button[type='submit']",
            "button:has-text('Continue')",
            "#new_link_auth_form input[type='submit']",
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0 and await btn.is_visible(timeout=1_000):
                    await btn.click(timeout=6_000)
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            await page.keyboard.press("Enter")

        # Form does a PATCH → page reloads with authenticated session
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(3_000)

        # Click toolbar area to give keyboard focus without hitting slide content
        await page.mouse.click(720, 30)
        await page.wait_for_timeout(500)

        print(f"Email gate cleared ({email})")

    # ── Slide info ─────────────────────────────────────────────────────────────

    async def _detect_title(self) -> Optional[str]:
        """Best-effort deck name: DocSend doc-name element → og:title → <title>."""
        candidates: list[str] = []

        # 1. The document-name element DocSend renders in its toolbar/header
        for sel in [
            ".toolbar-document-name",
            "[class*='document-name']",
            "[class*='doc-title']",
            "header h1",
        ]:
            try:
                t = await self.page.locator(sel).first.text_content(timeout=800)
                if t and t.strip():
                    candidates.append(t)
                    break
            except Exception:
                continue

        # 2. Open Graph title meta tag
        try:
            og = await self.page.evaluate(
                "() => document.querySelector('meta[property=\"og:title\"]')?.content || null"
            )
            if og:
                candidates.append(og)
        except Exception:
            pass

        # 3. The page <title>
        try:
            t = await self.page.title()
            if t:
                candidates.append(t)
        except Exception:
            pass

        for raw in candidates:
            cleaned = _clean_title(raw)
            if cleaned:
                return cleaned
        return None

    async def _detect_total(self) -> Optional[int]:
        info = await self._get_counter()
        if info:
            return info[1]
        # Fallback: count document-thumb-container data attributes
        n = await self.page.evaluate("""() =>
            document.querySelectorAll('[data-page-num]').length
        """)
        return n if n and n > 0 else None

    async def _get_counter(self) -> Optional[tuple[int, int]]:
        try:
            text = await self.page.locator(".toolbar-page-indicator").first.text_content(timeout=1_000)
            if text:
                info = _parse_counter(text)
                if info:
                    return info
        except Exception:
            pass
        # Broader search
        try:
            texts: list[str] = await self.page.evaluate("""() =>
                [...document.querySelectorAll('*')]
                    .map(el => el.textContent?.trim() ?? '')
                    .filter(t => /\\d+\\s*[/|]\\s*\\d+/.test(t) && t.length < 25)
            """)
            for t in texts:
                info = _parse_counter(t)
                if info and 1 <= info[0] <= info[1] <= 9_999:
                    return info
        except Exception:
            pass
        return None

    # ── Waiting ────────────────────────────────────────────────────────────────

    async def _wait_for_load(self) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        await self.page.wait_for_timeout(max(self.extra_wait_ms, 500))

    # ── Capture ────────────────────────────────────────────────────────────────

    async def _capture_slide(self, slide_num: int, imgs_before: int) -> bytes:
        """
        Try in order:
          1. Use a newly-intercepted large image (the slide CDN image).
          2. Find the biggest visible <img> or <canvas> and element-screenshot it.
          3. Crop the viewport to remove the toolbar.
        """
        out = self.output_dir / f"slide_{slide_num:03d}.png"
        page = self.page

        # -- Method 1: intercepted network image --
        new_imgs = self._all_large_images[imgs_before:]
        if new_imgs:
            # Pick the largest by byte size (most likely the slide, not a thumbnail)
            url, body = max(new_imgs, key=lambda x: len(x[1]))
            out.write_bytes(body)
            kb = len(body) // 1024
            print(f" [net {kb}KB]", end="", flush=True)
            return body

        # -- Method 2: largest visible <img> or <canvas> element screenshot --
        element_bytes = await self._element_screenshot(out)
        if element_bytes:
            return element_bytes

        # -- Method 3: viewport screenshot cropped below toolbar --
        toolbar_h = await page.evaluate("""() => {
            const tb = document.getElementById('toolbar') ||
                       document.querySelector('.presentation-toolbar');
            return tb ? tb.getBoundingClientRect().bottom : 60;
        }""")
        vp = page.viewport_size or {"width": 1440, "height": 900}
        clip = {
            "x": 0,
            "y": toolbar_h,
            "width": vp["width"],
            "height": vp["height"] - toolbar_h,
        }
        body = await page.screenshot(type="png", clip=clip)
        out.write_bytes(body)
        print(f" [viewport crop]", end="", flush=True)
        return body

    async def _element_screenshot(self, out: Path) -> Optional[bytes]:
        """Screenshot the largest visible img or canvas on the page."""
        page = self.page
        for selector in ["img", "canvas"]:
            elements = page.locator(selector)
            n = await elements.count()
            best = None
            best_area = 0.0
            for i in range(n):
                el = elements.nth(i)
                try:
                    if not await el.is_visible(timeout=300):
                        continue
                    bb = await el.bounding_box()
                    if bb and bb["width"] > 200 and bb["height"] > 150:
                        area = bb["width"] * bb["height"]
                        if area > best_area:
                            best_area = area
                            best = el
                except Exception:
                    continue
            if best:
                try:
                    body = await best.screenshot(type="png")
                    out.write_bytes(body)
                    bb = await best.bounding_box()
                    if bb:
                        print(f" [{int(bb['width'])}×{int(bb['height'])}px]", end="", flush=True)
                    return body
                except Exception:
                    pass
        return None

    # ── Navigation ─────────────────────────────────────────────────────────────

    async def _navigate_next(self) -> bool:
        """Go to next slide. Returns True if next button is disabled (end of deck)."""
        page = self.page

        # Click the toolbar area (top of page) to set focus without hitting
        # embedded iframes like YouTube videos which steal keyboard focus
        await page.mouse.click(720, 30)
        await page.wait_for_timeout(200)

        # DocSend toolbar uses right-arrow SVG button; try known selectors first
        for sel in [
            "button.js-document-next",
            ".js-document-next",
            "button[aria-label*='next' i]",
            "button[aria-label*='forward' i]",
            "[class*='next-btn']:not([disabled])",
            "button:has-text('›')",
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() == 0 or not await btn.is_visible(timeout=400):
                    continue
                if await btn.is_disabled(timeout=400):
                    return True
                await btn.click(timeout=3_000)
                await page.wait_for_timeout(900)
                return False
            except Exception:
                continue

        # Keyboard fallback (most reliable once focus is set)
        await page.keyboard.press("ArrowRight")
        await page.wait_for_timeout(900)
        return False


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="docsend_scraper",
        description="Capture every slide from a DocSend presentation as PNG files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 docsend_scraper.py https://docsend.com/view/abc123xyz
  python3 docsend_scraper.py https://docsend.com/view/abc123xyz --email me@company.com
  python3 docsend_scraper.py https://docsend.com/view/abc123xyz -o deck --scale 3
  python3 docsend_scraper.py https://docsend.com/view/abc123xyz --show-browser
        """,
    )
    ap.add_argument("url", help="DocSend presentation URL")
    ap.add_argument("-o", "--output", default="slides", metavar="DIR",
                    help="Output folder (default: slides/)")
    ap.add_argument("-e", "--email", metavar="EMAIL",
                    help="Email for gated presentations (prompted if omitted)")
    ap.add_argument("--headless", action="store_true", default=True,
                    help="Headless browser (default: True)")
    ap.add_argument("--show-browser", action="store_true", default=False,
                    help="Show the browser window (useful for debugging)")
    ap.add_argument("-s", "--scale", type=float, default=2.0, metavar="N",
                    help="Device pixel ratio for screenshot quality (default: 2.0)")
    ap.add_argument("-w", "--wait", type=int, default=0, metavar="MS",
                    help="Extra ms to wait after each slide loads (default: 0)")
    args = ap.parse_args()

    if not args.url.startswith("http"):
        ap.error("URL must start with http:// or https://")

    scraper = DocSendScraper(
        output_dir=args.output,
        email=args.email,
        headless=args.headless,
        scale=args.scale,
        extra_wait_ms=args.wait,
        show_browser=args.show_browser,
    )

    print(f"\ndocsend_scraper  scale={args.scale}×  headless={not args.show_browser}\n")

    try:
        total = asyncio.run(scraper.scrape(args.url))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)

    print(f"\n{'─'*50}")
    print(f"Captured {total} slide{'s' if total != 1 else ''} → {args.output}/")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    main()

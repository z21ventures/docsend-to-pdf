# docsend-to-pdf

> **Manual tool:** Run this on demand whenever you receive a DocSend link you need to save.

Converts any DocSend link into a downloadable PDF in under a minute. Z21 uses it to save and share founder pitch decks that are locked behind DocSend's viewer.

---

## Purpose & Workflow

Founders routinely share decks as DocSend links. Those links expire, track views, and block downloads. This tool bypasses the restriction by loading each slide in a headless browser, capturing the slide images directly from DocSend's CDN, and stitching them into a single PDF.

Run it whenever you receive a DocSend link worth keeping. The output is a single PDF — named after the deck's own title by default, or whatever file name you enter — that downloads straight to your browser.

---

## Step 1: Running the Tool

No installation required. The tool runs as a hosted web app.

1. Open [https://z21ventures-docsend-to-pdf-new.hf.space](https://z21ventures-docsend-to-pdf-new.hf.space) (you must be signed into Hugging Face as a `z21ventures` org member — the Space is private)
2. Enter the access code (ask the Z21 team; it's configured as the `ACCESS_CODE` secret on the Space)
3. Paste the DocSend URL into the **DocSend URL** field
4. If the deck requires an email to view, enter your Z21 work email in the **Email** field
5. *(Optional)* Set a **File name** — leave blank to auto-name the PDF after the deck's own title
6. Click **Download PDF**
7. Wait 30–60 seconds while the slides are captured (longer decks can take up to 2 minutes)
8. The PDF downloads automatically when done

> **Keep the URL and access code internal. Do not share outside the Z21 team.**

---

## Step 2: Finalising and Uploading

Use the **File name** field (or rename afterwards) so the saved PDF is descriptive:

```
[Company Name] - [Round] - [YYYY-MM].pdf
```

Example: `Acme - Seed - 2025-11.pdf`

Save it to the relevant company folder on the shared drive.

---

## Troubleshooting and Safety Tips

**"No slides were captured" error**
The URL is expired or the deck requires an email gate. Open the URL in your browser first to confirm it loads. If it prompts for an email, re-run with your work email filled in the **Email** field.

**The spinner keeps going and nothing downloads**
Decks with 30+ slides can take up to 3 minutes. Wait before retrying. If it still fails after 3 minutes, refresh the page and try once more. Persistent failures likely mean DocSend has changed its layout.

**The PDF is missing slides**
Some DocSend decks use a non-standard viewer that the scraper can't intercept from the CDN. Download what you have and manually screenshot the missing slides.

**"Scraping failed" error in the browser**
DocSend may have updated its front-end. File a bug at [github.com/z21ventures/docsend-to-pdf](https://github.com/z21ventures/docsend-to-pdf) with the failing URL (redact if sensitive).

**The access code is rejected**
The code is set as the `ACCESS_CODE` secret on the Space (Settings → Variables and secrets). Ask the Z21 team for the current value, or update the secret if you manage the Space.

---

## Technical Architecture

```
User browser
    │
    │  POST /scrape  {url, email}
    ▼
Flask app (app.py)
    │
    ▼
DocSendScraper (docsend_scraper.py)
    │
    ├── Playwright / headless Chromium
    │       ├── Dismiss Dropbox CCPA cookie banner
    │       ├── Fill email gate if present (#link_auth_form_email)
    │       ├── Detect total slide count from .toolbar-page-indicator
    │       └── Per slide loop:
    │               ├── [Primary]  Intercept CDN image from network response (>=20 KB)
    │               ├── [Fallback] Screenshot largest visible <img> or <canvas>
    │               └── [Fallback] Viewport screenshot cropped below toolbar
    │
    ├── slide_001.png ... slide_NNN.png  (tempfile.TemporaryDirectory)
    │
    ▼
img2pdf  (lossless PNG-to-PDF stitch)
    │
    ▼
Flask send_file  (stream deck.pdf to browser as attachment)
```

### `docsend_scraper.py`: slide capture engine

The scraper uses three capture strategies in priority order.

The first is network interception (`_on_response`). DocSend loads each slide as a full-resolution JPEG or PNG served from a CDN. Intercepting these directly gives the highest quality output without screenshot compression artifacts.

When no CDN image arrives (some decks render slides as canvas elements), the scraper falls back to screenshotting the largest visible `<img>` or `<canvas>` element by bounding-box area.

The final fallback is a full viewport screenshot cropped below the toolbar height, retrieved by querying the `#toolbar` element's bottom offset.

Navigation uses DocSend's next-slide button (`button.js-document-next`) where available, with `ArrowRight` keyboard press as a fallback. Stuck-detection runs on two independent signals: the slide counter not advancing between steps, and an MD5 hash match between consecutive captured images.

### `app.py`: Flask web layer

A thin wrapper around the scraper. Each `POST /scrape` request instantiates a `DocSendScraper`, runs the capture pipeline into a `tempfile.TemporaryDirectory`, converts the PNGs with `img2pdf`, and streams the result back as a file attachment. The temp directory is cleaned up automatically when the `with` block exits.

### `templates/index.html`: UI

Single-page form with no JavaScript framework dependencies. The submit handler fires `fetch()` against `/scrape`, shows a spinner while the scrape runs, then creates a blob URL and triggers a programmatic `<a>` click to download when the response arrives.

### `Dockerfile`: deployment

The container runs on Hugging Face Spaces on port 7860. The Chromium install is split across two stages deliberately: system dependencies are installed as `root`, then the Chromium browser binary is installed as uid 1000 (the HF Spaces runtime user). Installing the browser binary as root would place it in a path the runtime user can't access, causing Playwright to fail silently.

### Tech stack

| Dependency | Version | Purpose |
|---|---|---|
| Python | 3.11 | Runtime |
| Flask | unpinned | HTTP server and routing |
| Playwright | unpinned | Headless Chromium browser automation |
| img2pdf | unpinned | Lossless PNG-to-PDF stitching |
| Pillow | unpinned | Image processing in the screenshot pipeline |
| Docker | runtime | Containerisation for Hugging Face Spaces |

### Limitations

**DocSend layout changes.** The scraper targets specific CSS selectors (`button.js-document-next`, `.toolbar-page-indicator`, `#link_auth_form_email`) that DocSend can change at any time. A front-end update will degrade capture quality silently or fail entirely.

**Email-gated only.** The scraper handles DocSend email gates. Passcode-protected links are not supported and will produce zero slides.

**Sequential capture.** Slides are captured one at a time. A 50-slide deck takes roughly 60–90 seconds.

**Single-threaded server.** The Hugging Face Spaces deployment uses Flask's built-in dev server. Simultaneous requests from two users will queue or interfere with each other.

**Unpinned dependencies.** `requirements.txt` does not pin versions for Flask, Playwright, or img2pdf. A breaking update to any of these packages will fail silently on next container rebuild.

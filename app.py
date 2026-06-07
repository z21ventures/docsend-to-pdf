import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path

import img2pdf
from flask import Flask, render_template, request, send_file

from docsend_scraper import DocSendScraper, safe_filename

app = Flask(__name__)
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")

# Suppress Flask request logs and scraper stdout so URLs never appear in HF logs
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        code = request.form.get("access_code", "")
        if code == ACCESS_CODE:
            return render_template("index.html", auth=True, access_code=code, error=None)
        return render_template("index.html", auth=False, access_code=None, error="Incorrect access code.")
    return render_template("index.html", auth=False, access_code=None, error=None)


@app.route("/scrape", methods=["POST"])
def scrape():
    if request.form.get("access_code", "") != ACCESS_CODE:
        return "Unauthorized", 401

    url = request.form.get("url", "").strip()
    email = request.form.get("email", "").strip()
    custom_name = request.form.get("filename", "").strip()

    if not url.startswith("http"):
        return "Invalid URL — must start with https://", 400

    with tempfile.TemporaryDirectory() as tmpdir:
        scraper = DocSendScraper(
            output_dir=tmpdir,
            email=email or None,
            headless=True,
        )

        try:
            # Redirect stdout so scraper print() calls don't log URLs to HF
            import sys, io as _io
            _old_stdout = sys.stdout
            sys.stdout = _io.StringIO()
            try:
                total = asyncio.run(scraper.scrape(url))
            finally:
                sys.stdout = _old_stdout
        except Exception as e:
            return f"Scraping failed: {e}", 500

        png_files = sorted(Path(tmpdir).glob("slide_*.png"))
        if not png_files:
            return "No slides were captured. Check the URL or email.", 500

        try:
            from PIL import Image
            jpg_files = []
            for png in png_files:
                jpg_path = png.with_suffix(".jpg")
                img = Image.open(png).convert("RGB")
                img.save(jpg_path, "JPEG", quality=85, optimize=True)
                jpg_files.append(str(jpg_path))
            pdf_bytes = img2pdf.convert(jpg_files)
        except Exception as e:
            return f"PDF generation failed: {e}", 500

    # Filename priority: user's choice → detected deck title → "deck"
    download_name = safe_filename(custom_name or scraper.deck_title or "deck") + ".pdf"

    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    resp = send_file(
        buf,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/pdf",
    )
    # The browser downloads via fetch+blob, which ignores Content-Disposition,
    # so expose the chosen name in a custom header the front-end can read.
    resp.headers["X-Download-Filename"] = download_name
    resp.headers["Access-Control-Expose-Headers"] = "X-Download-Filename"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

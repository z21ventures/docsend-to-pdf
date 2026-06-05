import asyncio
import io
import os
import tempfile
from pathlib import Path

import img2pdf
from flask import Flask, render_template, request, send_file

from docsend_scraper import DocSendScraper, safe_filename

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def scrape():
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
            total = asyncio.run(scraper.scrape(url))
        except Exception as e:
            return f"Scraping failed: {e}", 500

        png_files = sorted(Path(tmpdir).glob("slide_*.png"))
        if not png_files:
            return "No slides were captured. Check the URL or email.", 500

        try:
            pdf_bytes = img2pdf.convert([str(f) for f in png_files])
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

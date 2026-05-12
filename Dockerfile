FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Chromium (needs root)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install-deps chromium

# Create non-root user to match Hugging Face Spaces runtime (uid 1000)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH="/home/user/.local/bin:$PATH"

# Install Chromium browser binary as the runtime user
RUN playwright install chromium

COPY --chown=user . .

EXPOSE 7860

CMD ["python", "app.py"]

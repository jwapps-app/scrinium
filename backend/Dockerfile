FROM python:3.12-slim

# ocrmypdf toolchain: Tesseract engine + PDF plumbing + text extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ghostscript \
        unpaper \
        qpdf \
        pngquant \
        poppler-utils \
        libzbar0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

RUN mkdir -p /data
ENV DATA_DIR=/data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# NOTE (security): this image runs as root so the bind-mounted /data stays
# writable regardless of the host's ownership — the pragmatic choice for a
# single-user NAS box behind the tunnel. To harden a shared deployment, add
# a non-root user whose uid matches the data volume's owner and set `user:`
# in compose; doing it here unconditionally would break existing NAS mounts.

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]

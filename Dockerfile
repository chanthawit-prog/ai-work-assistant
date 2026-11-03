# AI Work Assistant: dashboard + worker + the official Claude Code CLI in one container.
# Claude does the processing in the cloud with your subscription; this container only stores files,
# starts `claude -p` jobs and shows their progress.
FROM node:22-bookworm-slim

# MALLOC_ARENA_MAX=2: glibc otherwise keeps a memory arena per thread, which adds up on a 6 GB NAS
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    MALLOC_ARENA_MAX=2 \
    TZ=Asia/Bangkok

# poppler-utils: pdftotext and pdftoppm, so PDFs in the knowledge folder can be searched (runs only while indexing)
# tesseract + Thai and English models (about 40 MB): OCR for PDFs that are scans, used while indexing and when
# a job's files are prepared; one page at a time, two threads, so a 6 GB NAS stays usable
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv tini ca-certificates tzdata ripgrep poppler-utils \
      tesseract-ocr tesseract-ocr-tha tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

# Claude Code, installed unmodified from Anthropic's npm package (current versions install the native
# binary for this platform). Rebuild the image to update it.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

WORKDIR /opt/app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY tools ./tools

# HOME holds Claude Code's login and session history; mount it as a volume so it survives rebuilds.
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/assistant \
    DATA_DIR=/data \
    KNOWLEDGE_DIR=/data/knowledge
RUN mkdir -p /home/assistant /data/knowledge && chmod 0777 /home/assistant

EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)" || exit 1
ENTRYPOINT ["tini", "--"]
# no access log: the dashboard polls every few seconds and those lines would only fill the container log
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

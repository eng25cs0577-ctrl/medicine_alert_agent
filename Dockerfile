# Works on Render, Railway, Fly.io, Hugging Face Spaces (Docker)...
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=7860

RUN useradd -m -u 1000 app
WORKDIR /app
# The app user must own /app so the SQLite fallback can create data/medalert.db.
RUN mkdir -p /app/data && chown -R app:app /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
USER app

EXPOSE 7860
CMD ["sh", "-c", "uvicorn web:app --host 0.0.0.0 --port ${PORT}"]

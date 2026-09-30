FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY Scrapling /app/Scrapling
COPY verifier /app/verifier
RUN pip install --no-cache-dir "./Scrapling[fetchers]" "apify>=3,<5" "openpyxl>=3.1,<4" "tldextract>=5,<6"
RUN python -m playwright install --with-deps chromium
RUN python -m patchright install chromium

CMD ["python", "-m", "verifier.actor"]

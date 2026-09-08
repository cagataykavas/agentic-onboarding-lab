FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ONBOARDING_DATABASE_PATH=/data/onboarding.db

WORKDIR /app
COPY pyproject.toml ./
COPY onboarding.py ./
COPY service ./service
RUN pip install --no-cache-dir .

RUN addgroup --system onboarding \
    && adduser --system --ingroup onboarding --no-create-home onboarding \
    && mkdir -p /data \
    && chown onboarding:onboarding /data
VOLUME ["/data"]
EXPOSE 8000
USER onboarding
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "service.api:app", "--host", "0.0.0.0", "--port", "8000"]

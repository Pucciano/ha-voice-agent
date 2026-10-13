# OpenAI-compatible proxy in front of vLLM (compose/dev) or a cloud provider
# (Jetson, docs/jetson_setup.md, section 11). Built on the Jetson:
#   docker build -t aivi/llm-proxy:0.2.1 services/llm_proxy
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

# Wyoming speech-to-text server in front of an OpenAI-compatible
# transcription API (docs/jetson_setup.md, section 12). Built on the Jetson:
#   docker build -t aivi/stt-bridge:0.1.0 services/stt_bridge
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/stt-bridge

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Writes nothing; the audio stays in memory.
USER 1000:1000

EXPOSE 10300

ENTRYPOINT ["python", "-m", "app.main"]

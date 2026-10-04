# Recorder for wake word training takes that the satellites stream
# (docs/jetson_setup.md, section 10). Standard library only, so the image
# needs no network access after the base image has been pulled. Built on the
# Jetson:
#   docker build -t aivi/wake-word-recorder:0.1.0 services/wake_word_recorder
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/wake-word-recorder
COPY app ./app

# The capture drive belongs to uid 1000 (aivi on the Jetson).
USER 1000:1000

EXPOSE 10800

ENTRYPOINT ["python", "-m", "app.main"]

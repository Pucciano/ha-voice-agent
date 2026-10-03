# Wyoming relay that captures speech-to-text requests (docs/jetson_setup.md,
# section 9). Standard library only, so the image needs no network access
# after the base image has been pulled. Built on the Jetson:
#   docker build -t aivi/stt-capture:0.1.0 services/stt_capture
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/stt-capture
COPY app ./app

# The capture drive belongs to uid 1000 (aivi on the Jetson).
USER 1000:1000

EXPOSE 10300

ENTRYPOINT ["python", "-m", "app.main"]

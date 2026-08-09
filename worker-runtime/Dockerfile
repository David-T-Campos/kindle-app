FROM ubuntu:24.04

ARG CALIBRE_VERSION=9.12.0
ARG EPUBCHECK_VERSION=5.3.0
ENV DEBIAN_FRONTEND=noninteractive TZ=UTC CALIBRE_VERSION=${CALIBRE_VERSION} EPUBCHECK_VERSION=${EPUBCHECK_VERSION} PROFILE_VERSION=default-1 VALIDATOR_VERSION=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils unzip tzdata python3 fontconfig xdg-utils openjdk-21-jre-headless clamav libasound2t64 libegl1 libgl1 libopengl0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-xinerama0 libnss3 libxcomposite1 libxdamage1 libxrandr2 && rm -rf /var/lib/apt/lists/*
RUN freshclam
RUN curl -fsSLo /tmp/calibre.txz "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-x86_64.txz" && mkdir -p /opt/calibre && tar -xJf /tmp/calibre.txz -C /opt/calibre && rm /tmp/calibre.txz && /opt/calibre/calibre_postinstall
RUN curl -fsSLo /tmp/epubcheck.zip "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" && unzip -q /tmp/epubcheck.zip -d /opt && mv "/opt/epubcheck-${EPUBCHECK_VERSION}" /opt/epubcheck && rm /tmp/epubcheck.zip

RUN useradd --create-home --uid 10001 worker && mkdir -p /work && chown worker:worker /work
COPY --chown=worker:worker server.py /app/server.py
COPY --chown=worker:worker run_job.py /app/run_job.py
USER 10001
WORKDIR /work
ENV PORT=8080 HOME=/home/worker
EXPOSE 8080
ENTRYPOINT ["python3", "/app/server.py"]

# Hostile targets for the SSRF test bed (stdlib only): "public" web, internal service, fake metadata, DNS, UDP sink.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
FROM ${PYTHON_IMAGE}
COPY targets.py /srv/targets.py
ENTRYPOINT ["python", "-u", "/srv/targets.py"]

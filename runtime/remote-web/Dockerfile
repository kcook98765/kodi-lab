FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS wheel-build

WORKDIR /build
RUN python -m pip install --no-cache-dir uv==0.11.7 hatchling==1.29.0
COPY pyproject.toml uv.lock README.md ./
COPY migrations ./migrations
COPY src ./src
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /build/requirements.txt >/dev/null \
    && uv build --wheel --no-build-isolation --out-dir /wheels

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS installed-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KODI_LAB_DATA_ROOT=/var/lib/kodi-lab \
    KODI_LAB_RUNTIME_ROOT=/run/kodi-lab

COPY --from=wheel-build /build/requirements.txt /tmp/requirements.txt
COPY --from=wheel-build /wheels/*.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl \
    && python -m pip check \
    && rm -rf /tmp/requirements.txt /tmp/wheels \
    && groupadd --gid 10001 kodi-lab \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent kodi-lab \
    && install -d -m 0700 -o kodi-lab -g kodi-lab /var/lib/kodi-lab \
    && install -d -m 0750 -o root -g kodi-lab /run/kodi-lab

FROM installed-runtime AS web

ENV KODI_LAB_WEB_BIND_HOST=0.0.0.0 \
    KODI_LAB_REMOTE_CONTAINER=1

COPY runtime/remote-web/launch.sh /usr/local/bin/kodi-lab-remote-web
RUN chmod 0755 /usr/local/bin/kodi-lab-remote-web

USER kodi-lab
EXPOSE 8100
ENTRYPOINT ["/usr/local/bin/kodi-lab-remote-web"]

FROM installed-runtime AS broker

USER 0:0
ENTRYPOINT ["kodi-lab-broker"]

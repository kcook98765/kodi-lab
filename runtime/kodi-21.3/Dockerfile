FROM ubuntu:26.04@sha256:e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d

ARG DEBIAN_FRONTEND=noninteractive
ARG KODI_APT_VERSION=2:21.3+dfsg-1ubuntu1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        dbus-x11 \
        fonts-noto-core \
        fonts-roboto-unhinted \
        kodi="${KODI_APT_VERSION}" \
        kodi-bin="${KODI_APT_VERSION}" \
        kodi-data="${KODI_APT_VERSION}" \
        libgl1-mesa-dri \
        procps \
        x11-utils \
        xauth \
        xvfb \
    && test "$(dpkg-query -W -f='${Version}' kodi)" = "${KODI_APT_VERSION}" \
    && kodi --version | grep -F '21.3' \
    && rm -rf /var/lib/apt/lists/*

RUN usermod --login kodi --home /home/kodi --move-home --shell /bin/sh ubuntu \
    && groupmod --new-name kodi ubuntu \
    && usermod --groups kodi kodi \
    && install -d -o kodi -g kodi /home/kodi/.kodi/userdata /run/kodi \
    && install -d -m 1777 -o root -g root /tmp/.X11-unix

COPY --chown=kodi:kodi entrypoint.sh /usr/local/bin/kodi-lab-entrypoint
COPY --chown=kodi:kodi healthcheck.sh /usr/local/bin/kodi-lab-kodi-healthcheck
COPY --chown=kodi:kodi profile/guisettings.xml /home/kodi/.kodi/userdata/guisettings.xml
RUN chmod 0755 /usr/local/bin/kodi-lab-entrypoint \
    /usr/local/bin/kodi-lab-kodi-healthcheck

USER kodi
ENV HOME=/home/kodi \
    DISPLAY=:99 \
    XDG_RUNTIME_DIR=/run/kodi \
    LIBGL_ALWAYS_SOFTWARE=1 \
    MESA_LOADER_DRIVER_OVERRIDE=llvmpipe \
    GALLIUM_DRIVER=llvmpipe \
    PULSE_SERVER=none

HEALTHCHECK --interval=1s --timeout=2s --start-period=10s --retries=3 \
    CMD ["/usr/local/bin/kodi-lab-kodi-healthcheck"]

ENTRYPOINT ["/usr/local/bin/kodi-lab-entrypoint"]

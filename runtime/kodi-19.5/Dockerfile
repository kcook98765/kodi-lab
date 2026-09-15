FROM ubuntu:22.04@sha256:281c5745f657873d78e5531fc5ba8575f46ab7769b94550ac99543f122679986

ARG DEBIAN_FRONTEND=noninteractive

# The two package hashes come from the Team Kodi kodi-old-stable Jammy index,
# signed by Launchpad PPA key 04AE50726660A0D55355E966C8B28F649961BE33.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL \
        https://ppa.launchpadcontent.net/team-xbmc/kodi-old/ubuntu/pool/main/k/kodi/kodi_19.5+git20221224.1006-f8fdeb6b1b-0~jammy_all.deb \
        -o /tmp/kodi.deb \
    && curl -fsSL \
        https://ppa.launchpadcontent.net/team-xbmc/kodi-old/ubuntu/pool/main/k/kodi/kodi-bin_19.5+git20221224.1006-f8fdeb6b1b-0~jammy_amd64.deb \
        -o /tmp/kodi-bin.deb \
    && echo "7aaaa271143cffdd4509dba7f41df0669add970e6ecadc582441a43df83e722d  /tmp/kodi.deb" | sha256sum -c - \
    && echo "b48f7483f95fd4e336f00a5d765b1b9e86e6ae045bfd2e4bf1a4845afb370bf2  /tmp/kodi-bin.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends \
        /tmp/kodi.deb \
        /tmp/kodi-bin.deb \
        dbus-x11 \
        fonts-noto-core \
        fonts-roboto-unhinted \
        libgl1-mesa-dri \
        procps \
        x11-utils \
        xauth \
        xvfb \
    && test "$(dpkg-query -W -f='${Version}' kodi)" = "6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy" \
    && test "$(dpkg-query -W -f='${Version}' kodi-bin)" = "6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy" \
    && kodi --version | grep -F '19.5' \
    && rm -f /tmp/kodi.deb /tmp/kodi-bin.deb \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 kodi \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/sh kodi \
    && install -d -o kodi -g kodi \
        /home/kodi/.kodi \
        /home/kodi/.kodi/userdata \
        /run/kodi \
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

FROM ubuntu:26.04@sha256:e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d AS kodi22-builder

ARG DEBIAN_FRONTEND=noninteractive

RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        curl \
        ninja-build \
    && apt-get build-dep -y kodi \
    && curl -fsSL \
        "https://codeload.github.com/xbmc/xbmc/tar.gz/e513e0ff4331fc25fd2454659a9dd3e6b7670146" \
        -o /tmp/kodi-source.tar.gz \
    && echo "0a1f5c0910f39f21e7d7521835ae0eba6286cff1a6c665badf502443f8e4acd0  /tmp/kodi-source.tar.gz" | sha256sum -c - \
    && mkdir -p /usr/src/kodi \
    && tar -xzf /tmp/kodi-source.tar.gz -C /usr/src/kodi --strip-components=1 \
    && test "$(awk '/^VERSION_MAJOR / {print $2}' /usr/src/kodi/version.txt)" = "22" \
    && test "$(awk '/^VERSION_MINOR / {print $2}' /usr/src/kodi/version.txt)" = "0" \
    && test "$(awk '/^VERSION_TAG / {print $2}' /usr/src/kodi/version.txt)" = "BETA2" \
    && rm /tmp/kodi-source.tar.gz

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ccache \
        libunistring-dev \
        meson \
        nasm \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,id=kodi22b2-build,target=/tmp/kodi-build \
    cmake -S /usr/src/kodi -B /tmp/kodi-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/kodi \
        -DCORE_PLATFORM_NAME=x11 \
        -DAPP_RENDER_SYSTEM=gl \
        -DENABLE_GOLD=OFF \
        -DENABLE_INTERNAL_EXIV2=ON \
        -DENABLE_INTERNAL_FFMPEG=ON \
        -DENABLE_INTERNAL_NLOHMANNJSON=ON \
        -DENABLE_INTERNAL_TAGLIB=ON \
        -DENABLE_TESTING=OFF \
        -DENABLE_VAAPI=OFF \
        -DENABLE_VDPAU=OFF \
        -DENABLE_BLURAY_JAR=OFF \
    && cmake --build /tmp/kodi-build --parallel 4 \
    && cmake --install /tmp/kodi-build \
    && /opt/kodi/bin/kodi --version | grep -F '22.0-BETA2' \
    && install -d /opt/kodi/share/kodi-lab \
    && dpkg-query -W -f='${binary:Package}\t${Version}\n' \
        | LC_ALL=C sort > /opt/kodi/share/kodi-lab/build-packages.tsv

FROM ubuntu:26.04@sha256:e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d

ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.source="https://github.com/xbmc/xbmc" \
      org.opencontainers.image.revision="e513e0ff4331fc25fd2454659a9dd3e6b7670146" \
      org.opencontainers.image.version="22.0b2-Piers"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        dbus-x11 \
        fonts-noto-core \
        fonts-roboto-unhinted \
        kodi="2:21.3+dfsg-1ubuntu1" \
        kodi-bin="2:21.3+dfsg-1ubuntu1" \
        kodi-data="2:21.3+dfsg-1ubuntu1" \
        libgl1-mesa-dri \
        procps \
        x11-utils \
        xauth \
        xvfb \
    && apt-get purge -y kodi kodi-bin kodi-data \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kodi22-builder /opt/kodi /opt/kodi

RUN /opt/kodi/bin/kodi --version | grep -F '22.0-BETA2' \
    && ldd /opt/kodi/lib/kodi/kodi-x11 > /tmp/kodi-ldd \
    && ! grep -F 'not found' /tmp/kodi-ldd \
    && rm /tmp/kodi-ldd \
    && dpkg-query -W -f='${binary:Package}\t${Version}\n' \
        | LC_ALL=C sort > /opt/kodi/share/kodi-lab/runtime-packages.tsv \
    && usermod --login kodi --home /home/kodi --move-home --shell /bin/sh ubuntu \
    && groupmod --new-name kodi ubuntu \
    && usermod --groups kodi kodi \
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

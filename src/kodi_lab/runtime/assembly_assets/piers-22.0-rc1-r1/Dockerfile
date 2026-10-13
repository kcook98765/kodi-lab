FROM ubuntu:26.04@sha256:e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d AS support
LABEL io.kodi-lab.operation-id="@@OPERATION@@" io.kodi-lab.experimental="true"
ENV DEBIAN_FRONTEND=noninteractive
COPY support/ /tmp/reviewed-debs/
COPY assets/support-manifest.json /usr/share/kodi-lab/support-manifest.json
RUN set -eu; rm -rf /var/lib/apt/lists/*; \
    apt-get -y --no-install-recommends -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=/dev/null install /tmp/reviewed-debs/*.deb; \
    dpkg --purge apt libapt-pkg7.0 gpgv ubuntu-keyring; \
    dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' | sort > /usr/share/kodi-lab/installed-packages.tsv; \
    rm -rf /tmp/reviewed-debs /var/lib/apt/lists/* /var/cache/apt/archives/*.deb; \
    test "$(id -u ubuntu)" = 1000; test "$(id -g ubuntu)" = 1000; \
    usermod -l kodi -d /home/kodi -m -s /bin/sh ubuntu; groupmod -n kodi ubuntu; usermod -G '' kodi; \
    test "$(id -G kodi)" = 1000; \
    install -d -o 1000 -g 1000 -m 0700 /home/kodi/.kodi /home/kodi/.kodi/userdata /run/kodi; \
    install -d -m 1777 -o root -g root /tmp/.X11-unix
# Copy the merged support filesystem, omitting DEB archives and acquisition layers.
FROM scratch
COPY --from=support / /
COPY payload/app/files/ /app/
COPY payload/platform/files/ /opt/fdo/
COPY payload/gl/files/ /opt/mesa/
COPY assets/launcher.sh /usr/local/bin/kodi-lab-launcher
COPY assets/supervisor.py assets/profile_init.py assets/healthcheck.py assets/xvfb_guard.py /usr/local/lib/kodi-lab/
COPY assets/xrandr.sh /usr/share/kodi-lab/xrandr.sh
COPY assets/guisettings.xml /usr/share/kodi-lab/guisettings.xml
RUN set -eu; \
    test ! -e /opt/fdo/lib/x86_64-linux-gnu/addons; test ! -L /opt/fdo/lib/x86_64-linux-gnu/addons; \
    test -d /app/lib/kodi/addons; \
    ln -s /app/lib/kodi/addons /opt/fdo/lib/x86_64-linux-gnu/addons; \
    test ! -e /opt/fdo/lib/x86_64-linux-gnu/kodi-xrandr; test ! -L /opt/fdo/lib/x86_64-linux-gnu/kodi-xrandr; \
    cp /usr/share/kodi-lab/xrandr.sh /opt/fdo/lib/x86_64-linux-gnu/kodi-xrandr; \
    chmod 0755 /usr/local/bin/kodi-lab-launcher /opt/fdo/lib/x86_64-linux-gnu/kodi-xrandr; \
    /opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 --library-path /app/lib:/opt/mesa/lib:/opt/fdo/lib/x86_64-linux-gnu:/opt/fdo/lib/x86_64-linux-gnu/pulseaudio:/opt/fdo/lib --list /app/lib/kodi/kodi.bin > /usr/share/kodi-lab/loader-resolution.txt
LABEL io.kodi-lab.experimental="true" io.kodi-lab.installation-status="inactive" \
    io.kodi-lab.operation-id="@@OPERATION@@" io.kodi-lab.recipe-id="piers-22.0-rc1-r1" \
    io.kodi-lab.recipe-digest="@@RECIPE_DIGEST@@" io.kodi-lab.acquisition-digest="@@ACQUISITION_DIGEST@@" \
    io.kodi-lab.support-digest="@@SUPPORT_DIGEST@@" io.kodi-lab.assets-digest="@@ASSET_DIGEST@@" \
    org.opencontainers.image.revision="28ea2eac1eb7af8fdcbd2672d933ce87f594be79" org.opencontainers.image.version="22.0-rc.1"
USER 1000:1000
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/home/kodi DISPLAY=:99 XDG_RUNTIME_DIR=/run/kodi \
    LIBGL_ALWAYS_SOFTWARE=1 MESA_LOADER_DRIVER_OVERRIDE=llvmpipe \
    GALLIUM_DRIVER=llvmpipe PULSE_SERVER=none PYTHONDONTWRITEBYTECODE=1
HEALTHCHECK --interval=2s --timeout=3s --start-period=20s --retries=3 CMD ["/usr/bin/python3", "-I", "/usr/local/lib/kodi-lab/healthcheck.py"]
ENTRYPOINT ["/usr/bin/python3", "/usr/local/lib/kodi-lab/supervisor.py"]
CMD []

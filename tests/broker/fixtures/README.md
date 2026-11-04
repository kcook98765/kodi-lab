The `docker_desktop_broker.json` fixture contains selected, sanitized Docker SDK inspection metadata from a fresh Windows 11 / Ubuntu 24.04 WSL2 / Docker Desktop broker at main `5dc14b76f364135f9c5d23525975555fcccf188e` (Engine 29.8.2). It was captured from inside the broker through its existing socket, not reconstructed from `docker ps` labels.

Container, image, engine and Compose project identities are replaced with fixture values. The actual bind-source aliases, distro, requested `HostConfig.Binds`, resolved `Mounts`, modes, permissions and descriptor shapes are retained. No environment, authentication or database contents are included.

The source paths are the exact socket proxy alias and SHA-256(`/proc/1/net/route`) route alias for the broker-inspected distro. Tests also change each role independently and retain literal Linux and mixed descriptor coverage.

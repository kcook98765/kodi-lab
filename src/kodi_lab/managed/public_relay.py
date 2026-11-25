"""One disposable relay for a fixed HTTPS origin and authenticated Kodi control."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from functools import partial
from pathlib import Path

HOST = "kcook98765.github.io"
PORT = 3128
PAGES_ADDRESS = "185.199.108.153"
CONTROL_PORTS = (8080, 8765)
POLICY = Path("/policy")


def approved_connect(headers: bytes) -> bool:
    return headers.split(b"\r\n", 1)[0] in {
        f"CONNECT {HOST}:443 HTTP/1.0".encode(),
        f"CONNECT {HOST}:443 HTTP/1.1".encode(),
    }


def approved_tls_hello(record: bytes) -> bool:
    """Accept one bounded TLS ClientHello with the exact approved server name."""
    data = memoryview(record)
    offset = 0

    def take(length: int) -> memoryview:
        nonlocal offset
        if offset + length > len(data):
            raise ValueError("truncated ClientHello")
        chunk = data[offset : offset + length]
        offset += length
        return chunk

    def number(length: int) -> int:
        return int.from_bytes(take(length))

    try:
        if number(1) != 1 or number(3) != len(data) - 4:
            return False
        take(34)
        take(number(1))
        take(number(2))
        take(number(1))
        if number(2) != len(data) - offset:
            return False
        found = False
        while offset < len(data):
            kind = number(2)
            size = number(2)
            value = take(size)
            if kind == 0:
                name = HOST.encode()
                expected = (
                    (len(name) + 3).to_bytes(2) + b"\0" + len(name).to_bytes(2) + name
                )
                if found or value != expected:
                    return False
                found = True
            if kind == 0xFE0D:
                # An encrypted ClientHello can carry a different inner name.
                return False
        return found
    except ValueError:
        return False


async def pump(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
    while data := await source.read(32768):
        target.write(data)
        await target.drain()


async def transfer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    address: str,
    port: int,
    timeout: float,
    *,
    initial: bytes = b"",
) -> None:
    async with asyncio.timeout(timeout):
        remote, outgoing = await asyncio.open_connection(address, port)
        try:
            if initial:
                outgoing.write(initial)
                await outgoing.drain()
            tasks = (
                asyncio.create_task(pump(reader, outgoing)),
                asyncio.create_task(pump(remote, writer)),
            )
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            outgoing.close()


async def run() -> None:
    config = POLICY / "relay.json"
    async with asyncio.timeout(10):
        while not config.exists():
            await asyncio.sleep(0.05)
    record = json.loads(config.read_text())
    deadline: float = record["deadline"]
    slots = asyncio.Semaphore(12)
    # Pin one documented GitHub Pages IPv4 address. The runtime and this
    # relay have no external DNS requirement. Kodi validates the original TLS.
    print(f"public repository relay ready for {HOST} via {PAGES_ADDRESS}", flush=True)

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(5):
                headers = await reader.readuntil(b"\r\n\r\n")
            if not approved_connect(headers):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if slots.locked() or time.time() >= deadline:
                return
            async with (
                slots,
                asyncio.timeout_at(
                    asyncio.get_running_loop().time() + max(0, deadline - time.time())
                ),
            ):
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                async with asyncio.timeout(5):
                    prefix = await reader.readexactly(5)
                    size = int.from_bytes(prefix[3:])
                    if prefix[0] != 22 or not 40 <= size <= 12288:
                        return
                    hello = await reader.readexactly(size)
                if approved_tls_hello(hello):
                    await transfer(
                        reader, writer, PAGES_ADDRESS, 443, 120, initial=prefix + hello
                    )
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()

    def kodi_address() -> str:
        address = ipaddress.IPv4Address((POLICY / "kodi-ip").read_text())
        if address not in ipaddress.IPv4Network(record["subnet"]):
            raise ValueError("Kodi address outside the isolated network")
        return str(address)

    async def runtime_lifetime() -> None:
        # Allow native startup and existing managed skin restarts to settle.
        # If Kodi disappears, terminate the helper and all accepted tunnels.
        loop = asyncio.get_running_loop()
        last_seen = loop.time()
        while loop.time() - last_seen < 40:
            try:
                async with asyncio.timeout(1):
                    _, probe = await asyncio.open_connection(
                        kodi_address(), CONTROL_PORTS[0]
                    )
                    probe.close()
                last_seen = loop.time()
            except (OSError, ValueError, TimeoutError):
                pass
            await asyncio.sleep(2)

    async def control(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int
    ) -> None:
        try:
            if writer.get_extra_info("peername")[0] != record["gateway"]:
                return
            await transfer(reader, writer, kodi_address(), port, 75)
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()

    listeners = [
        await asyncio.start_server(proxy, record["internal"], PORT, limit=4096)
    ]
    for port in CONTROL_PORTS:
        listeners.append(
            await asyncio.start_server(
                partial(control, port=port), record["control"], port, limit=32768
            )
        )
    try:
        async with asyncio.timeout(max(0, deadline - time.time())):
            await runtime_lifetime()
    except TimeoutError:
        pass
    finally:
        for listener in listeners:
            listener.close()


if __name__ == "__main__":
    asyncio.run(run())

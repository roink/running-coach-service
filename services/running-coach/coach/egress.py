"""Public-only forward proxy with DNS pinning and bounded CONNECT tunnels."""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from coach.lifecycle import run_service


def public_addresses(addresses):
    result = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_unspecified
            or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped)
        ):
            raise ValueError("Non-public destination")
        result.append(address)
    if not result:
        raise ValueError("Empty DNS response")
    return list(dict.fromkeys(result))


async def relay(reader, writer):
    total = 0
    while True:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=90)
        if not chunk:
            break
        total += len(chunk)
        if total > 128 * 1024 * 1024:
            raise ValueError("Tunnel byte budget exceeded")
        writer.write(chunk)
        await writer.drain()


class Proxy:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(16)

    async def handle(self, reader, writer):
        upstream = None
        try:
            async with asyncio.timeout(900), self.semaphore:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
                if len(header) > 16_384:
                    raise ValueError("Oversized header")
                lines = header.decode("iso-8859-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("Unsupported HTTP version")
                if method == "CONNECT":
                    parsed = urlsplit("//" + target)
                    port = parsed.port
                    if port != 443 or parsed.path or parsed.username or parsed.password:
                        raise ValueError("Unsupported CONNECT destination")
                    request = None
                elif method in {"GET", "HEAD"}:
                    parsed = urlsplit(target)
                    port = parsed.port or 80
                    if parsed.scheme != "http" or port != 80 or parsed.username or parsed.password:
                        raise ValueError(
                            "Only public HTTP GET/HEAD and HTTPS CONNECT are supported"
                        )
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    # Only a small header set is forwarded; never hop-by-hop or proxy credentials.
                    forwarded = []
                    for line in lines[1:]:
                        name, separator, value = line.partition(":")
                        if separator and name.lower() in {"accept", "user-agent", "range"}:
                            forwarded.append(name + ":" + value)
                    request = (
                        f"{method} {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\n"
                        + "\r\n".join(forwarded)
                        + "\r\nConnection: close\r\n\r\n"
                    ).encode("iso-8859-1")
                else:
                    raise ValueError("Unsupported proxy method")
                hostname = parsed.hostname
                if not hostname or len(hostname) > 253:
                    raise ValueError("Invalid destination")
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    hostname, port, type=socket.SOCK_STREAM
                )
                candidates = public_addresses([item[4][0] for item in addresses])
                # No second DNS lookup: connect to the validated numeric address.
                errors = []
                for address in candidates:
                    try:
                        remote_reader, upstream = await asyncio.wait_for(
                            asyncio.open_connection(address, port), timeout=10
                        )
                        break
                    except OSError as error:
                        errors.append(error)
                else:
                    raise OSError("No public destination connected")
                if request is None:
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    upstream.write(request)
                    await upstream.drain()
                tasks = [
                    asyncio.create_task(relay(reader, upstream)),
                    asyncio.create_task(relay(remote_reader, writer)),
                ]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (
            ValueError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
        finally:
            if upstream:
                upstream.close()
            writer.close()
            await writer.wait_closed()


async def serve():
    proxy = Proxy()
    server = await asyncio.start_server(
        proxy.handle, "0.0.0.0", int(os.getenv("PORT", "3128")), limit=16_384
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(run_service(serve()))

#!/usr/bin/env python3
"""Forward HTTP connections to one gateway without changing their bytes."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import os
import signal

LOG = logging.getLogger("proxy")
BUFFER_SIZE = 64 * 1024


async def _copy_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    while data := await reader.read(BUFFER_SIZE):
        writer.write(data)
        await writer.drain()
    # EOF ends one write direction; the opposite direction can keep streaming.
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()


class ProxyServer:
    """One upstream TCP connection per client, with bounded stream buffers."""

    def __init__(
        self, upstream_ip: str, upstream_port: int, connect_timeout: float = 10.0
    ) -> None:
        self.upstream_ip = str(ipaddress.ip_address(upstream_ip))
        if not 1 <= upstream_port <= 65535:
            raise ValueError("upstream_port must be between 1 and 65535")
        if not math.isfinite(connect_timeout) or connect_timeout <= 0:
            raise ValueError("connect_timeout must be a finite positive number")
        self.upstream_port = upstream_port
        self.connect_timeout = connect_timeout
        self._connections: set[asyncio.Task[None]] = set()
        self._closing = False

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        upstream_writer: asyncio.StreamWriter | None = None
        transfers: list[asyncio.Task[None]] = []
        clean_eof = False
        peer = writer.get_extra_info("peername")
        try:
            if self._closing:
                return
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_ip, self.upstream_port),
                timeout=self.connect_timeout,
            )
            LOG.debug("Connected client %s to gateway", peer)
            transfers = [
                asyncio.create_task(_copy_stream(reader, upstream_writer)),
                asyncio.create_task(_copy_stream(upstream_reader, writer)),
            ]
            # FIN closes only one direction. Either peer can still receive data.
            await asyncio.gather(*transfers)
            clean_eof = True
        except (OSError, asyncio.TimeoutError) as exc:
            LOG.warning(
                "Connection %s to gateway %s:%s failed: %s",
                peer, self.upstream_ip, self.upstream_port,
                str(exc) or type(exc).__name__,
            )
        finally:
            for transfer in transfers:
                transfer.cancel()
            writers = [writer]
            if upstream_writer is not None:
                writers.append(upstream_writer)
            # Close before awaiting so cancellation cannot leave sockets open.
            for stream in writers:
                if clean_eof:
                    stream.close()
                else:
                    stream.transport.abort()
            try:
                await asyncio.gather(*transfers, return_exceptions=True)
                # Normal EOF must flush buffered bytes even for slow clients.
                await asyncio.gather(
                    *(stream.wait_closed() for stream in writers),
                    return_exceptions=True,
                )
            finally:
                for stream in writers:
                    stream.transport.abort()
                self._connections.discard(task)

    async def close(self) -> None:
        """Stop all active forwarding connections."""
        self._closing = True
        connections = tuple(self._connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)


def _ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请输入有效的 IPv4 或 IPv6 地址") from exc


def _port(value: str) -> int:
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("端口必须是 1 到 65535 的整数")


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
        if math.isfinite(timeout) and timeout > 0:
            return timeout
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("连接超时必须是有限的正数（秒）")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="原样转发 HTTP 请求到 gateway",
        epilog="必须设置环境变量 GATEWAY_IP 和 GATEWAY_PORT，指定 gateway 的 IP 和端口。",
    )
    parser.add_argument(
        "--listen-host", type=_ip,
        default=os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0").strip(),
        help="代理监听 IP（默认读取 PROXY_LISTEN_HOST，未设置时为 0.0.0.0）",
    )
    parser.add_argument(
        "--listen-port", type=_port,
        default=os.environ.get("PROXY_LISTEN_PORT", "9000").strip(),
        help="代理监听端口（默认读取 PROXY_LISTEN_PORT，未设置时为 9000）",
    )
    parser.add_argument(
        "--connect-timeout", type=_timeout, default=10.0,
        help="连接 gateway 的超时秒数（默认 10），请求和流式响应不设超时",
    )
    parser.add_argument("--verbose", action="store_true", help="显示连接调试日志")
    args = parser.parse_args(argv)
    for attribute, env_name, parse_value in (
        ("ip", "GATEWAY_IP", _ip),
        ("port", "GATEWAY_PORT", _port),
    ):
        value = os.environ.get(env_name, "").strip()
        if not value:
            parser.error(f"必须设置非空环境变量 {env_name}")
        try:
            setattr(args, attribute, parse_value(value))
        except argparse.ArgumentTypeError as exc:
            parser.error(f"环境变量 {env_name}: {exc}")
    return args


async def serve(args: argparse.Namespace) -> None:
    proxy = ProxyServer(args.ip, args.port, args.connect_timeout)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    server: asyncio.Server | None = None
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
            except NotImplementedError:
                # asyncio.run still handles Ctrl+C on platforms without this API.
                pass
        server = await asyncio.start_server(
            proxy.handle_connection, args.listen_host, args.listen_port
        )
        LOG.info(
            "Listening on %s:%s -> gateway %s:%s",
            args.listen_host, args.listen_port, args.ip, args.port,
        )
        await stop.wait()
    finally:
        if server is not None:
            server.close()
        await proxy.close()
        if server is not None:
            # Python 3.12+ also waits for accepted connections here.
            await server.wait_closed()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        LOG.error("Cannot start proxy: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

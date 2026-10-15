"""Exercise the proxy over real local TCP connections without dependencies."""

import asyncio
import gzip
import signal
import unittest
from argparse import Namespace
from collections.abc import Awaitable, Callable
from unittest.mock import patch

import proxy as proxy_module
from proxy import ProxyServer


GatewayHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.gateway_handler: GatewayHandler = self.unexpected_connection
        self.gateway_errors: list[BaseException] = []
        self.gateway_tasks: set[asyncio.Task] = set()
        self.gateway_writers: set[asyncio.StreamWriter] = set()
        self.client_writers: list[asyncio.StreamWriter] = []

        self.gateway_server = await asyncio.start_server(
            self.dispatch_gateway, "127.0.0.1", 0
        )
        gateway_port = self.gateway_server.sockets[0].getsockname()[1]
        self.proxy = ProxyServer("127.0.0.1", gateway_port, connect_timeout=1.0)
        self.proxy_server = await asyncio.start_server(
            self.proxy.handle_connection, "127.0.0.1", 0
        )
        self.proxy_port = self.proxy_server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.proxy_server.close()
        self.gateway_server.close()
        for writer in self.client_writers:
            writer.close()
        await asyncio.wait_for(self.proxy.close(), timeout=3)
        for writer in tuple(self.gateway_writers):
            writer.close()
        pending = tuple(self.gateway_tasks)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=1)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for writer in self.client_writers:
            try:
                await writer.wait_closed()
            except OSError:
                pass
        # Python 3.12+ Server.wait_closed() includes accepted connections, so
        # release their writers and tasks before waiting for either server.
        await asyncio.wait_for(self.proxy_server.wait_closed(), timeout=3)
        await asyncio.wait_for(self.gateway_server.wait_closed(), timeout=3)
        if self.gateway_errors:
            raise self.gateway_errors[0]

    async def unexpected_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.fail("No gateway handler was installed for this test")

    async def dispatch_gateway(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self.gateway_tasks.add(task)
        self.gateway_writers.add(writer)
        try:
            await self.gateway_handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.gateway_errors.append(error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.gateway_writers.discard(writer)
            self.gateway_tasks.discard(task)

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", self.proxy_port), timeout=3
        )
        self.client_writers.append(writer)
        return reader, writer

    async def read_exactly(self, reader: asyncio.StreamReader, size: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(size), timeout=3)

    async def read_to_eof(self, reader: asyncio.StreamReader) -> bytes:
        return await asyncio.wait_for(reader.read(), timeout=3)

    async def exchange(self, request: bytes, response: bytes) -> None:
        async def gateway(reader, writer):
            self.assertEqual(await self.read_exactly(reader, len(request)), request)
            writer.write(response)
            await writer.drain()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        writer.write(request)
        await writer.drain()
        self.assertEqual(await self.read_to_eof(reader), response)
        writer.close()

    async def test_http_methods_and_error_or_redirect_statuses_are_unchanged(self):
        cases = (
            ("GET", "/", "200 OK"),
            ("POST", "/submit", "201 Created"),
            ("PUT", "/resource", "302 Found"),
            ("PATCH", "/resource", "307 Temporary Redirect"),
            ("DELETE", "/resource", "401 Unauthorized"),
            ("OPTIONS", "*", "403 Forbidden"),
            ("TRACE", "/trace", "404 Not Found"),
            ("PROPFIND", "/webdav", "500 Internal Server Error"),
            ("CONNECT", "example.test:443", "502 Bad Gateway"),
        )
        for method, target, status in cases:
            with self.subTest(method=method, status=status):
                request = (
                    f"{method} {target} HTTP/1.1\r\n"
                    "Host: original.example:3210\r\n"
                    "Content-Length: 0\r\n\r\n"
                ).encode("ascii")
                response = (
                    f"HTTP/1.1 {status}\r\n"
                    "Content-Length: 4\r\n"
                    "Location: /unfollowed?x=1\r\n"
                    "Connection: close\r\n\r\nbody"
                ).encode("ascii")
                await self.exchange(request, response)

    async def test_encoded_target_headers_and_gzip_bytes_are_preserved(self):
        request_body = gzip.compress(bytes(range(256)) * 32, mtime=0)
        response_body = gzip.compress(bytes(reversed(range(256))) * 32, mtime=0)
        request = (
            b"POST /api/%2Fitem/%7e/%E4%B8%AD?tag=one&tag=two&empty=&x=%2B+ HTTP/1.1\r\n"
            b"Host: gateway.external:8123\r\n"
            b"Authorization: Bearer unchanged-token\r\n"
            b"Cookie: session=one; theme=dark\r\n"
            b"X-Repeat: first\r\nX-Repeat: second\r\n"
            b"Connection: keep-alive, X-Connection-Data\r\n"
            b"X-Connection-Data: preserve-this-too\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Encoding: gzip\r\n"
            + f"Content-Length: {len(request_body)}\r\n\r\n".encode("ascii")
            + request_body
        )
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Encoding: gzip\r\n"
            b"Set-Cookie: session=two; HttpOnly\r\n"
            b"Set-Cookie: preference=light; Path=/\r\n"
            b"X-Repeat: alpha\r\nX-Repeat: beta\r\n"
            b"Connection: close, X-Response-Data\r\n"
            b"X-Response-Data: untouched\r\n"
            + f"Content-Length: {len(response_body)}\r\n\r\n".encode("ascii")
            + response_body
        )
        await self.exchange(request, response)

    async def test_chunk_extensions_and_trailers_are_preserved(self):
        request = (
            b"POST /upload HTTP/1.1\r\nHost: original.example\r\n"
            b"Transfer-Encoding: chunked\r\nTrailer: Upload-Checksum\r\n\r\n"
            b"4;name=one\r\n\x00\xffab\r\n"
            b"3\r\nxyz\r\n"
            b"0\r\nUpload-Checksum: raw-value\r\n\r\n"
        )
        response = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Trailer: Response-Checksum\r\nConnection: close\r\n\r\n"
            b"3;source=gateway\r\n\xff\x00z\r\n"
            b"0\r\nResponse-Checksum: original-value\r\n\r\n"
        )
        await self.exchange(request, response)

    async def test_large_binary_bodies_are_transferred_completely(self):
        request_body = bytes(range(256)) * 1024
        response_body = bytes(reversed(range(256))) * 1536
        request = (
            b"POST /large-upload HTTP/1.1\r\nHost: original.example\r\n"
            b"Content-Type: application/octet-stream\r\n"
            + f"Content-Length: {len(request_body)}\r\n\r\n".encode("ascii")
            + request_body
        )
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            b"Connection: close\r\n"
            + f"Content-Length: {len(response_body)}\r\n\r\n".encode("ascii")
            + response_body
        )
        await self.exchange(request, response)

    async def test_upload_and_sse_response_stream_before_request_finishes(self):
        request_start = (
            b"POST /events HTTP/1.1\r\nHost: original.example\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"5\r\nfirst\r\n"
        )
        request_end = b"6\r\nsecond\r\n0\r\nUpload-Checksum: complete\r\n\r\n"
        first_event = b"data: first\n\n"
        second_event = b"data: second\n\n"
        response_start = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            + f"{len(first_event):x}\r\n".encode("ascii")
            + first_event
            + b"\r\n"
        )
        response_end = (
            f"{len(second_event):x}\r\n".encode("ascii")
            + second_event
            + b"\r\n0\r\n\r\n"
        )

        async def gateway(reader, writer):
            self.assertEqual(
                await self.read_exactly(reader, len(request_start)), request_start
            )
            writer.write(response_start)
            await writer.drain()
            self.assertEqual(
                await self.read_exactly(reader, len(request_end)), request_end
            )
            writer.write(response_end)
            await writer.drain()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        writer.write(request_start)
        await writer.drain()
        # Receiving this response is a prerequisite for releasing the next upload
        # chunk: buffering either direction causes a deterministic timeout.
        self.assertEqual(
            await self.read_exactly(reader, len(response_start)), response_start
        )
        writer.write(request_end)
        await writer.drain()
        self.assertEqual(await self.read_to_eof(reader), response_end)

    async def test_keepalive_head_204_and_cookie_isolation(self):
        requests = (
            b"HEAD /head HTTP/1.1\r\nHost: original.example\r\n"
            b"Cookie: first=request\r\n\r\n",
            b"GET /empty HTTP/1.1\r\nHost: original.example\r\n\r\n",
            b"GET /binary HTTP/1.1\r\nHost: original.example\r\n\r\n",
        )
        responses = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 12345\r\n"
            b"Set-Cookie: should-not-be-injected=yes\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nX-Empty: yes\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n"
            b"Connection: close\r\n\r\n\x00\xffab",
        )
        accepted_connections = 0

        async def gateway(reader, writer):
            nonlocal accepted_connections
            accepted_connections += 1
            for request, response in zip(requests, responses):
                self.assertEqual(
                    await self.read_exactly(reader, len(request)), request
                )
                writer.write(response)
                await writer.drain()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        for request, response in zip(requests, responses):
            writer.write(request)
            await writer.drain()
            self.assertEqual(
                await self.read_exactly(reader, len(response)), response
            )
        self.assertEqual(await self.read_to_eof(reader), b"")
        self.assertEqual(accepted_connections, 1)

    async def test_upgrade_continues_forwarding_binary_data(self):
        request = (
            b"GET /upgrade HTTP/1.1\r\nHost: original.example\r\n"
            b"Connection: Upgrade\r\nUpgrade: custom-stream\r\n\r\n"
        )
        response = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Connection: Upgrade\r\nUpgrade: custom-stream\r\n\r\n"
        )
        client_data = b"\x00\xff\x80client\r\n\r\n\x01"
        gateway_data = b"\x81\x00gateway\xff\x00\r\n"

        async def gateway(reader, writer):
            self.assertEqual(await self.read_exactly(reader, len(request)), request)
            writer.write(response)
            await writer.drain()
            self.assertEqual(
                await self.read_exactly(reader, len(client_data)), client_data
            )
            writer.write(gateway_data)
            await writer.drain()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        writer.write(request)
        await writer.drain()
        self.assertEqual(await self.read_exactly(reader, len(response)), response)
        writer.write(client_data)
        await writer.drain()
        self.assertEqual(await self.read_to_eof(reader), gateway_data)

    async def test_client_half_close_still_receives_gateway_response(self):
        request = b"POST /eof HTTP/1.0\r\n\r\nbody-delimited-by-eof"
        response = b"HTTP/1.0 200 OK\r\n\r\nresponse-after-client-eof"

        async def gateway(reader, writer):
            self.assertEqual(await self.read_to_eof(reader), request)
            writer.write(response)
            await writer.drain()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        writer.write(request)
        await writer.drain()
        self.assertTrue(writer.can_write_eof())
        writer.write_eof()
        self.assertEqual(await self.read_to_eof(reader), response)

    async def test_gateway_half_close_still_receives_remaining_client_upload(self):
        request_start = b"POST /duplex-eof HTTP/1.0\r\n\r\nfirst-part"
        request_end = b"remaining-upload-after-gateway-eof"
        response = b"HTTP/1.0 200 OK\r\n\r\nresponse-before-upload-finishes"
        upload_complete = asyncio.Event()

        async def gateway(reader, writer):
            self.assertEqual(
                await self.read_exactly(reader, len(request_start)), request_start
            )
            writer.write(response)
            await writer.drain()
            self.assertTrue(writer.can_write_eof())
            writer.write_eof()
            self.assertEqual(await self.read_to_eof(reader), request_end)
            upload_complete.set()

        self.gateway_handler = gateway
        reader, writer = await self.connect()
        writer.write(request_start)
        await writer.drain()
        # The gateway ends only its write side. Its read side must remain usable.
        self.assertEqual(await self.read_to_eof(reader), response)
        writer.write(request_end)
        await writer.drain()
        writer.write_eof()
        await asyncio.wait_for(upload_complete.wait(), timeout=3)

    async def test_truncated_gateway_response_is_forwarded_without_rewriting(self):
        request = b"GET /truncated HTTP/1.1\r\nHost: original.example\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nshort-body"
        await self.exchange(request, response)

    async def test_gateway_eof_closes_client_connection(self):
        connected = asyncio.Event()

        async def gateway(reader, writer):
            connected.set()

        self.gateway_handler = gateway
        reader, _ = await self.connect()
        await asyncio.wait_for(connected.wait(), timeout=3)
        self.assertEqual(await self.read_to_eof(reader), b"")

    async def test_connection_failure_closes_client_without_synthetic_http(self):
        self.gateway_server.close()
        await self.gateway_server.wait_closed()
        reader, _ = await self.connect()
        self.assertEqual(await self.read_to_eof(reader), b"")

    async def test_close_terminates_active_connections(self):
        connected = asyncio.Event()
        gateway_disconnected = asyncio.Event()

        async def gateway(reader, writer):
            connected.set()
            self.assertEqual(await self.read_to_eof(reader), b"")
            gateway_disconnected.set()

        self.gateway_handler = gateway
        reader, _ = await self.connect()
        await asyncio.wait_for(connected.wait(), timeout=3)
        await asyncio.wait_for(self.proxy.close(), timeout=3)
        self.assertEqual(await self.read_to_eof(reader), b"")
        await asyncio.wait_for(gateway_disconnected.wait(), timeout=3)

    async def test_close_during_normal_eof_cleanup_closes_both_writers(self):
        request = b"GET /cleanup HTTP/1.0\r\n\r\n"
        response = b"HTTP/1.0 200 OK\r\n\r\ncomplete-response"
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleanup_writers: set[asyncio.StreamWriter] = set()
        cleanup_tasks: set[asyncio.Task] = set()
        gateway_port = self.gateway_server.sockets[0].getsockname()[1]
        original_wait_closed = asyncio.StreamWriter.wait_closed

        async def controlled_wait_closed(writer):
            local = writer.get_extra_info("sockname")
            peer = writer.get_extra_info("peername")
            is_proxy_writer = (
                local is not None and local[1] == self.proxy_port
            ) or (peer is not None and peer[1] == gateway_port)
            if is_proxy_writer:
                cleanup_writers.add(writer)
                cleanup_tasks.add(asyncio.current_task())
                if len(cleanup_writers) == 2:
                    cleanup_started.set()
                await allow_cleanup.wait()
            await original_wait_closed(writer)

        async def gateway(reader, writer):
            self.assertEqual(await self.read_to_eof(reader), request)
            writer.write(response)
            await writer.drain()

        self.gateway_handler = gateway
        with patch.object(asyncio.StreamWriter, "wait_closed", controlled_wait_closed):
            try:
                reader, writer = await self.connect()
                writer.write(request)
                await writer.drain()
                writer.write_eof()
                self.assertEqual(await self.read_to_eof(reader), response)
                await asyncio.wait_for(cleanup_started.wait(), timeout=3)
                closing = asyncio.create_task(self.proxy.close())
                # Let close() request cancellation while cleanup is held at its
                # first await, then release the gate without a timing-based sleep.
                await asyncio.sleep(0)
                allow_cleanup.set()
                await asyncio.wait_for(closing, timeout=3)
                self.assertEqual(len(cleanup_writers), 2)
                self.assertTrue(all(w.is_closing() for w in cleanup_writers))
                self.assertTrue(all(task.done() for task in cleanup_tasks))
            finally:
                allow_cleanup.set()

    async def test_serve_sigterm_stops_with_an_active_connection(self):
        listening = asyncio.Event()
        gateway_connected = asyncio.Event()
        gateway_disconnected = asyncio.Event()
        signal_callbacks = {}
        serve_server = None
        original_start_server = asyncio.start_server
        loop = asyncio.get_running_loop()

        async def gateway(reader, writer):
            gateway_connected.set()
            self.assertEqual(await self.read_to_eof(reader), b"")
            gateway_disconnected.set()

        async def capture_listener(*args, **kwargs):
            nonlocal serve_server
            serve_server = await original_start_server(*args, **kwargs)
            listening.set()
            return serve_server

        def capture_signal(signum, callback, *args):
            signal_callbacks[signum] = lambda: callback(*args)

        self.gateway_handler = gateway
        args = Namespace(
            ip="127.0.0.1",
            port=self.gateway_server.sockets[0].getsockname()[1],
            connect_timeout=1.0,
            listen_host="127.0.0.1",
            listen_port=0,
        )
        with (
            patch.object(asyncio, "start_server", capture_listener),
            patch.object(loop, "add_signal_handler", capture_signal),
            patch.object(loop, "remove_signal_handler"),
        ):
            serving = asyncio.create_task(proxy_module.serve(args))
            try:
                await asyncio.wait_for(listening.wait(), timeout=3)
                port = serve_server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=3
                )
                self.client_writers.append(writer)
                await asyncio.wait_for(gateway_connected.wait(), timeout=3)
                self.assertIn(signal.SIGTERM, signal_callbacks)
                # Invoke the installed shutdown callback without changing this
                # process's actual signal handlers or sending an OS signal.
                signal_callbacks[signal.SIGTERM]()
                await asyncio.wait_for(serving, timeout=3)
                self.assertEqual(await self.read_to_eof(reader), b"")
                await asyncio.wait_for(gateway_disconnected.wait(), timeout=3)
            finally:
                if not serving.done():
                    serving.cancel()
                await asyncio.wait_for(
                    asyncio.gather(serving, return_exceptions=True), timeout=3
                )
                if serve_server is not None:
                    serve_server.close()
                    await asyncio.wait_for(serve_server.wait_closed(), timeout=3)


if __name__ == "__main__":
    unittest.main()

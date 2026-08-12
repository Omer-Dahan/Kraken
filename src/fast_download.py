#!/usr/bin/env python3
"""
Parallel (multi-connection) Telegram downloader ("FastTelethon").
Downloads Telegram files using multiple concurrent MTProto connections to bypass per-connection speed limits.
Note: Relies on Telethon 1.x internal APIs (_call, _get_dc, etc.).
"""

import asyncio
import inspect
import logging
import math
import os
from typing import AsyncGenerator, Awaitable, BinaryIO, List, Optional, Union

from telethon import TelegramClient, utils
from telethon.crypto import AuthKey
from telethon.errors import FloodPremiumWaitError, FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import (
    Document,
    InputDocumentFileLocation,
    InputFileLocation,
    InputPeerPhotoFileLocation,
    InputPhotoFileLocation,
)

log = logging.getLogger("fast_download")

# Telegram starts handing out FLOOD_WAIT well before this; 20 is the practical ceiling.
MAX_CONNECTIONS = 20

# Telegram throttles downloads for accounts without a Premium subscription and answers
# upload.GetFile with FLOOD_PREMIUM_WAIT_X instead of data. The wait is per account, not
# per connection, so it must be waited out rather than routed around.
MAX_FLOOD_RETRIES = 6
MAX_FLOOD_SLEEP = 300  # Refuse to sit on a download for more than 5 minutes per chunk.

TypeLocation = Union[
    Document,
    InputDocumentFileLocation,
    InputPeerPhotoFileLocation,
    InputFileLocation,
    InputPhotoFileLocation,
]


async def _maybe_await(value):
    """Awaits the value if the progress callback happens to be a coroutine function."""
    if inspect.isawaitable(value):
        return await value
    return value


class DownloadSender:
    """One dedicated MTProto connection fetching every Nth chunk of the file."""

    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file: TypeLocation,
        offset: int,
        limit: int,
        stride: int,
        count: int,
        on_throttle: Optional[callable] = None,
    ) -> None:
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count
        self.on_throttle = on_throttle

    async def _call_with_backoff(self):
        """
        Issues the GetFile request, absorbing Telegram's rate limits.

        flood_sleep_threshold=0 stops Telethon from silently sleeping through short waits,
        so every throttle event surfaces here and can be reported to the caller.
        """
        for attempt in range(MAX_FLOOD_RETRIES):
            try:
                return await self.client._call(self.sender, self.request, flood_sleep_threshold=0)
            except (FloodPremiumWaitError, FloodWaitError) as e:
                # FloodPremiumWaitError does NOT subclass FloodWaitError - it inherits from
                # FloodError - so both have to be named explicitly.
                is_premium_limit = isinstance(e, FloodPremiumWaitError)
                wait = getattr(e, "seconds", 0) or 0
                if wait > MAX_FLOOD_SLEEP:
                    raise
                reason = (
                    "non-Premium download throttle (FLOOD_PREMIUM_WAIT)"
                    if is_premium_limit
                    else "FLOOD_WAIT"
                )
                log.warning(
                    f"{reason}: sleeping {wait}s at offset {self.request.offset} "
                    f"(attempt {attempt + 1}/{MAX_FLOOD_RETRIES})"
                )
                if self.on_throttle:
                    self.on_throttle(wait, is_premium_limit)
                await asyncio.sleep(wait + 1)
        raise Exception(f"Gave up after {MAX_FLOOD_RETRIES} rate-limit retries.")

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        result = await self._call_with_backoff()
        self.remaining -= 1
        self.request.offset += self.stride
        return result.bytes

    def disconnect(self) -> Awaitable[None]:
        return self.sender.disconnect()


class ParallelTransferrer:
    """Spreads a single file download across several MTProto connections to one DC."""

    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        # An auth key is only reusable on the DC it was issued for; otherwise each
        # new sender has to import an exported authorization for the target DC.
        self.auth_key: Optional[AuthKey] = (
            None
            if dc_id and self.client.session.dc_id != dc_id
            else self.client.session.auth_key
        )
        self.senders: Optional[List[DownloadSender]] = None

    async def _cleanup(self) -> None:
        if self.senders:
            await asyncio.gather(*[sender.disconnect() for sender in self.senders])
        self.senders = None

    @staticmethod
    def _get_connection_count(file_size: int, max_count: int = MAX_CONNECTIONS) -> int:
        """Scales connections with file size - tiny files do not need 10 sockets."""
        full_size = 100 * 1024 * 1024
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        if not self.auth_key:
            log.debug(f"Exporting authorization for DC {self.dc_id}")
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(
        self,
        file: TypeLocation,
        index: int,
        part_size: int,
        stride: int,
        part_count: int,
        on_throttle: Optional[callable] = None,
    ) -> DownloadSender:
        return DownloadSender(
            self.client,
            await self._create_sender(),
            file,
            index * part_size,
            part_size,
            stride,
            part_count,
            on_throttle=on_throttle,
        )

    async def _init_download(
        self,
        connections: int,
        file: TypeLocation,
        part_count: int,
        part_size: int,
        on_throttle: Optional[callable] = None,
    ) -> None:
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        # The first sender is created alone: it performs the authorization export that
        # the rest then reuse, so creating them all at once would race on the auth key.
        self.senders = [
            await self._create_download_sender(
                file, 0, part_size, connections * part_size, get_part_count(), on_throttle
            ),
            *await asyncio.gather(
                *[
                    self._create_download_sender(
                        file, i, part_size, connections * part_size, get_part_count(), on_throttle
                    )
                    for i in range(1, connections)
                ]
            ),
        ]

    async def download(
        self,
        file: TypeLocation,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
        on_throttle: Optional[callable] = None,
    ) -> AsyncGenerator[bytes, None]:
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = math.ceil(file_size / part_size)
        log.info(
            f"Starting parallel download: {connection_count} connections, "
            f"{part_count} parts of {part_size} bytes"
        )
        await self._init_download(connection_count, file, part_count, part_size, on_throttle)

        try:
            part = 0
            while part < part_count:
                # Every sender fetches its next chunk concurrently, but the results are
                # awaited in sender order so the byte stream stays sequential.
                tasks = [self.loop.create_task(sender.next()) for sender in self.senders]
                exhausted = False
                try:
                    for task in tasks:
                        data = await task
                        if not data:
                            exhausted = True
                            break
                        yield data
                        part += 1
                finally:
                    # Cancel any tasks that were not awaited (e.g. on early exit, exception, or cancellation)
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                if exhausted:
                    # A sender ran out of assigned parts; nothing left to interleave.
                    break
        finally:
            log.info("Parallel download finished, closing extra connections")
            await self._cleanup()


async def fast_download(
    client: TelegramClient,
    location: TypeLocation,
    out: BinaryIO,
    progress_callback: callable = None,
    connection_count: Optional[int] = None,
    on_throttle: Optional[callable] = None,
) -> BinaryIO:
    """
    Downloads a Document over several parallel connections into an open binary file.

    `location` must be a Document (message.document), not a Message.
    `on_throttle(seconds, is_premium_limit)` is called whenever Telegram rate-limits a chunk.
    """
    size = location.size
    dc_id, input_location = utils.get_input_location(location)
    downloader = ParallelTransferrer(client, dc_id)

    # Buffer 4MB in RAM before offloading to disk thread to minimize thread pool churn
    buffer = bytearray()
    BUFFER_SIZE = 4 * 1024 * 1024

    async for chunk in downloader.download(
        input_location, size, connection_count=connection_count, on_throttle=on_throttle
    ):
        buffer.extend(chunk)
        if len(buffer) >= BUFFER_SIZE:
            await asyncio.to_thread(out.write, bytes(buffer))
            buffer.clear()
        if progress_callback:
            try:
                current_bytes = out.tell() + len(buffer)
                await _maybe_await(progress_callback(current_bytes, size))
            except Exception as e:
                log.warning(f"Progress callback failed: {e}")

    if buffer:
        await asyncio.to_thread(out.write, bytes(buffer))
        buffer.clear()

    return out


async def fast_download_to_path(
    client: TelegramClient,
    document: Document,
    path: str,
    progress_callback: callable = None,
    connection_count: Optional[int] = None,
    on_throttle: Optional[callable] = None,
) -> str:
    """Convenience wrapper: parallel-downloads a Document straight to a filesystem path."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as out:
        await fast_download(
            client, document, out, progress_callback=progress_callback,
            connection_count=connection_count, on_throttle=on_throttle
        )
    # Verify that the file on disk matches the expected byte count. A mismatch means
    # the connection dropped mid-transfer or Telegram served a truncated response -
    # either way the file is unusable and download_message_media's BaseException handler
    # will clean it up and fall back to the other account.
    expected = getattr(document, "size", None)
    if isinstance(expected, int) and expected > 0:
        actual = os.path.getsize(path)
        if actual != expected:
            raise IOError(
                f"Size mismatch after download: expected {expected} bytes, got {actual}"
            )
    return path


def premium_connection_count(is_premium: bool, configured: int, non_premium_cap: int) -> int:
    """
    Caps the connection count for accounts without Telegram Premium.

    Telegram's throttle is applied per account, so opening more sockets past the cap does
    not buy throughput - it only multiplies the FLOOD_PREMIUM_WAIT responses.
    """
    if is_premium:
        return configured
    return min(configured, non_premium_cap)

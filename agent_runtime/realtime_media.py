"""Bounded, serialized provider writes; controls and audio precede the latest video."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class MediaPacket:
    header: dict
    payload: bytes
    generation: int
    received_at: float


class RealtimeMediaQueue:
    def __init__(self, *, max_audio_chunks: int = 20, max_audio_bytes: int = 64_000):
        self.audio: deque[MediaPacket] = deque()
        self.video: MediaPacket | None = None
        self.control: MediaPacket | None = None
        self.audio_bytes = 0
        self.max_audio_chunks = max_audio_chunks
        self.max_audio_bytes = max_audio_bytes
        self.closed = False
        self._ready = asyncio.Event()

    def put_audio(self, packet: MediaPacket) -> bool:
        if (self.closed or len(self.audio) >= self.max_audio_chunks
                or self.audio_bytes + len(packet.payload) > self.max_audio_bytes):
            return False
        self.audio.append(packet)
        self.audio_bytes += len(packet.payload)
        self._ready.set()
        return True

    def put_video(self, packet: MediaPacket) -> bool:
        """Return whether an unsent video was replaced."""
        if self.closed:
            return False
        replaced = self.video is not None
        self.video = packet
        self._ready.set()
        return replaced

    def interrupt(self, generation: int) -> None:
        if self.closed:
            return
        self.audio.clear()
        self.audio_bytes = 0
        self.video = None
        self.control = MediaPacket({}, b"", generation, time.perf_counter())
        self._ready.set()

    def close(self) -> None:
        self.closed = True
        self.audio.clear()
        self.audio_bytes = 0
        self.video = None
        self.control = None
        self._ready.set()

    async def run(self, forward: Callable[[str, MediaPacket], Awaitable[None]]) -> None:
        while not self.closed:
            await self._ready.wait()
            if self.closed:
                return
            if self.control is not None:
                packet, self.control = self.control, None
                kind = "interrupt"
            elif self.audio:
                packet = self.audio.popleft()
                self.audio_bytes -= len(packet.payload)
                kind = "audio"
            elif self.video is not None:
                packet, self.video = self.video, None
                kind = "video"
            else:
                self._ready.clear()
                continue
            # Only this worker writes to the provider. A slow write cannot hold
            # the gateway receive loop; cancel/heartbeat can still be received.
            await forward(kind, packet)

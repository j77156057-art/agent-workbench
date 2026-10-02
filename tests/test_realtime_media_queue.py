import asyncio
import time

from agent_runtime.realtime_media import MediaPacket, RealtimeMediaQueue


def packet(sequence, payload=b'pcm', generation=0):
    return MediaPacket({'sequence': sequence}, payload, generation, time.perf_counter())


def test_latest_video_and_fifo_audio_with_cancel_priority():
    async def run():
        queue = RealtimeMediaQueue()
        assert queue.put_video(packet(1)) is False
        assert queue.put_video(packet(2)) is True
        assert queue.put_audio(packet(3))
        queue.interrupt(1)
        assert queue.audio_bytes == 0
        queue.put_audio(packet(4, generation=1))
        queue.put_audio(packet(5, generation=1))
        queue.put_video(packet(6, generation=1))
        seen = []

        async def forward(kind, item):
            seen.append((kind, item.header.get('sequence'), item.generation))
            if kind == 'video':
                queue.close()

        await asyncio.wait_for(queue.run(forward), 1)
        assert seen == [('interrupt', None, 1), ('audio', 4, 1), ('audio', 5, 1), ('video', 6, 1)]
        assert not queue.put_audio(packet(7))
        assert queue.video is None

    asyncio.run(run())


def test_slow_video_keeps_control_reception_responsive_and_does_not_replay_old_queue():
    async def run():
        queue = RealtimeMediaQueue()
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def forward(kind, item):
            seen.append((kind, item.header.get('sequence')))
            if kind == 'video' and item.header['sequence'] == 1:
                entered.set()
                await release.wait()
            if kind == 'audio':
                queue.close()

        queue.put_video(packet(1))
        task = asyncio.create_task(queue.run(forward))
        await entered.wait()
        for i in range(2, 500):
            queue.put_video(packet(i))
        queue.put_audio(packet(500))
        queue.interrupt(1)
        queue.put_audio(packet(501, generation=1))
        release.set()
        await asyncio.wait_for(task, 1)
        assert seen == [('video', 1), ('interrupt', None), ('audio', 501)]

    asyncio.run(run())


def test_audio_queue_is_bounded_by_both_chunks_and_bytes():
    queue = RealtimeMediaQueue(max_audio_chunks=2, max_audio_bytes=8)
    assert queue.put_audio(packet(1, b'1234'))
    assert not queue.put_audio(packet(2, b'12345'))
    assert queue.put_audio(packet(3, b'1234'))
    assert not queue.put_audio(packet(4, b'1'))
    assert queue.audio_bytes == 8
    assert len(queue.audio) == 2
    queue.close()
    assert queue.audio_bytes == 0


def test_close_wakes_idle_worker():
    async def run():
        queue = RealtimeMediaQueue()

        async def forward(*args):
            raise AssertionError('closed queue forwarded a packet')

        task = asyncio.create_task(queue.run(forward))
        await asyncio.sleep(0)
        queue.close()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())

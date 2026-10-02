import assert from 'node:assert/strict'
import { test } from 'node:test'
import { LiveFrameEncoder } from '../src/workbench/liveFrameEncoder.ts'

function fixture(timeout = 1000) {
  const worker = { terminated: false, onmessage: null, onerror: null, onmessageerror: null,
    postMessage(packet, transfer) { this.packet = packet; this.transfer = transfer },
    terminate() { this.terminated = true } }
  return { worker, encoder: new LiveFrameEncoder(() => worker, timeout),
    bitmap: () => ({ closed: false, close() { this.closed = true } }) }
}

test('worker transfers the bitmap and resolves only the matching frame', async () => {
  const { encoder, worker, bitmap } = fixture()
  const frame = bitmap()
  const pending = encoder.encode(frame, {})
  assert.equal(worker.transfer[0], frame)
  worker.onmessage({ data: { id: 999, result: {} } })
  const result = { image: new Blob(['jpeg']) }
  worker.onmessage({ data: { id: worker.packet.id, result } })
  assert.equal(await pending, result)
  encoder.dispose()
  assert.equal(worker.terminated, true)
})

test('stop rejects outstanding encoding and terminates the worker', async () => {
  const { encoder, worker, bitmap } = fixture()
  const pending = encoder.encode(bitmap(), {})
  encoder.dispose()
  await assert.rejects(pending, /会话已停止/)
  assert.equal(worker.terminated, true)
  const next = bitmap()
  await assert.rejects(encoder.encode(next, {}), /不可用/)
  assert.equal(next.closed, true)
})

test('timeout and worker errors disable background encoding so callers can fall back', async () => {
  const { encoder, worker, bitmap } = fixture(5)
  await assert.rejects(encoder.encode(bitmap(), {}), /超时/)
  assert.equal(worker.terminated, true)
  assert.equal(encoder.available(), false)
  const second = fixture()
  const pending = second.encoder.encode(second.bitmap(), {})
  second.worker.onerror()
  await assert.rejects(pending, /编码失败/)
  assert.equal(second.encoder.available(), false)
})

test('busy worker rejects extra frames and closes their bitmaps', async () => {
  const { encoder, bitmap } = fixture()
  const pending = encoder.encode(bitmap(), {})
  const extra = bitmap()
  await assert.rejects(encoder.encode(extra, {}), /上一帧/)
  assert.equal(extra.closed, true)
  encoder.dispose()
  await assert.rejects(pending)
})

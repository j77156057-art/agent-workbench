import assert from 'node:assert/strict'
import { test } from 'node:test'
import { shouldAnalyzeVisualFrame } from '../src/workbench/liveVisionSampling.ts'

const frame = (colors, width = 1280, height = 720) => ({
  pixels: Uint8Array.from(colors.flatMap(color => Array.isArray(color) ? color : [color, color, color])), width, height,
})

test('small encoding noise does not repeatedly call the vision model', () => {
  const baseline = frame(Array(576).fill(100))
  const noisy = frame(Array(576).fill(102))
  assert.equal(shouldAnalyzeVisualFrame(baseline, noisy, 1000, 2000), false)
})

test('live dedup refreshes static frames and compares accumulated changes to the last sent frame', () => {
  const baseline = frame(Array(2304).fill(100))
  assert.equal(shouldAnalyzeVisualFrame(baseline, baseline, 1000, 5999, 5000), false)
  assert.equal(shouldAnalyzeVisualFrame(baseline, baseline, 1000, 6000, 5000), true)
  assert.equal(shouldAnalyzeVisualFrame(baseline, baseline, 1000, 10_999, 10_000), false)
  assert.equal(shouldAnalyzeVisualFrame(baseline, baseline, 1000, 11_000, 10_000), true)
  assert.equal(shouldAnalyzeVisualFrame(baseline, frame(Array(2304).fill(102)), 1000, 2000), false)
  assert.equal(shouldAnalyzeVisualFrame(baseline, frame(Array(2304).fill(104)), 1000, 3000), true)
})

test('localized UI changes and periodic checks still reach the model', () => {
  const baseline = frame(Array(576).fill(100))
  const changed = Array(576).fill(100)
  changed.fill(170, 0, 8)
  assert.equal(shouldAnalyzeVisualFrame(baseline, frame(changed), 1000, 2000), true)
  const red = frame(Array(576).fill([255, 0, 0]))
  const sameBrightnessGreen = frame(Array(576).fill([0, 131, 0]))
  assert.equal(shouldAnalyzeVisualFrame(red, sameBrightnessGreen, 1000, 2000), true)
  assert.equal(shouldAnalyzeVisualFrame(baseline, baseline, 1000, 21_000), true)
  assert.equal(shouldAnalyzeVisualFrame(baseline, frame(Array(576).fill(100), 1920), 1000, 2000), true)
})

"""Real browser JPEG/worker tests with synthetic frames, no camera or model API."""
import functools
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_background_and_fallback_encoding_refresh_dedup_focus_and_force(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    from agent_runtime.visual_acceptance import _edge_binary

    node = shutil.which('node')
    compiler = ROOT / 'frontend/node_modules/typescript/bin/tsc'
    assets = list((ROOT / 'web/assets').glob('liveFrameEncoder.worker-*.js'))
    browser_path = _edge_binary()
    if not browser_path or not node or not compiler.exists() or not assets:
        pytest.skip('Requires an installed browser and a frontend build with the worker asset')
    compiled = subprocess.run([node, str(compiler),
        str(ROOT / 'frontend/src/workbench/liveFrameEncoding.ts'),
        str(ROOT / 'frontend/src/workbench/liveVisualWindow.ts'), '--target', 'es2020',
        '--module', 'esnext', '--moduleResolution', 'node', '--strict', '--outDir', str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def translate_path(self, path):
            result = super().translate_path(path)
            # TypeScript emits extensionless ESM imports; serve them as JS.
            if not Path(result).suffix and Path(result + '.js').exists():
                return result + '.js'
            return result

        def do_GET(self):
            if self.path == '/worker.js':
                body = assets[0].read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<!doctype html><title>Live frame test</title>')
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path=browser_path, headless=True)
            try:
                page = browser.new_page()
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                result = page.evaluate("""async () => {
                  const { encodeLiveFrame } = await import('/liveFrameEncoding.js');
                  const { createLiveVisualWindow } = await import('/liveVisualWindow.js');
                  const source = new OffscreenCanvas(1280, 720);
                  const context = source.getContext('2d');
                  const worker = new Worker('/worker.js');
                  let id = 0;
                  const background = async options => {
                    const bitmap = await createImageBitmap(source);
                    return new Promise((resolve, reject) => {
                      const timer = setTimeout(() => reject(new Error('worker timeout')), 4000);
                      worker.onerror = error => { clearTimeout(timer); reject(new Error(error.message)); };
                      worker.onmessage = ({ data }) => {
                        clearTimeout(timer);
                        data.error ? reject(new Error(data.error)) : resolve(data.result);
                      };
                      worker.postMessage({ id: ++id, bitmap, options }, [bitmap]);
                    });
                  };
                  const canvases = { frame: document.createElement('canvas'),
                    focus: document.createElement('canvas'), sample: document.createElement('canvas') };
                  const fallback = options => encodeLiveFrame(source, 1280, 720, options, canvases);
                  const verify = async encode => {
                    context.fillStyle = '#888'; context.fillRect(0, 0, 1280, 720);
                    const options = { maxEdge: 960, quality: .8, focus: null, previous: null,
                      lastSentAt: 0, capturedAt: 1000, refreshMs: 5000, force: false };
                    const first = await encode(options);
                    if (!(first.image instanceof Blob) || first.image.type !== 'image/jpeg') throw new Error('JPEG missing');
                    const decoded = await createImageBitmap(first.image);
                    if (decoded.width !== 960 || decoded.height !== 540) throw new Error('resize failed');
                    decoded.close();
                    options.previous = first.sample; options.lastSentAt = 1000; options.capturedAt = 2000;
                    const same = await encode(options);
                    if (same.image !== null) throw new Error('static frame encoded');
                    options.capturedAt = 6000;
                    if (!(await encode(options)).image) throw new Error('periodic refresh missing');
                    options.capturedAt = 2500; options.force = true;
                    if (!(await encode(options)).image) throw new Error('voice/anomaly refresh missing');
                    options.force = false;
                    context.fillStyle = '#f00'; context.fillRect(0, 0, 100, 150);
                    const changed = await encode(options);
                    if (!changed.image) throw new Error('localized change missed');
                    options.focus = { x: .5, y: .5, width: .25, height: .25 };
                    options.previous = null;
                    const focus = await encode(options);
                    const crop = await createImageBitmap(focus.focusImage);
                    if (crop.width !== 320 || crop.height !== 180) throw new Error('focus crop lost original resolution');
                    crop.close();
                    const timeline = createLiveVisualWindow();
                    options.focus = null; options.force = true; options.previous = null;
                    context.fillStyle = '#888'; context.fillRect(0, 0, 1280, 720);
                    options.capturedAt = 1000;
                    const baseline = await encode(options);
                    timeline.push(baseline); timeline.uploaded(baseline, 1000, baseline.image.size);
                    context.fillStyle = '#f00'; context.fillRect(300, 200, 400, 200);
                    options.capturedAt = 1125; options.previous = baseline.sample;
                    const popup = await encode(options); timeline.push(popup);
                    if (!popup.focusImage) throw new Error('automatic detail missing');
                    context.fillStyle = '#888'; context.fillRect(0, 0, 1280, 720);
                    options.capturedAt = 1250; options.previous = popup.sample;
                    const restored = await encode(options); timeline.push(restored);
                    const selected = timeline.select(1300);
                    if (selected.key.capturedAt !== 1125 || selected.current.capturedAt !== 1250)
                      throw new Error('flash disappeared from evidence');
                    options.previous = null;
                    options.context = { before: selected.before, key: selected.key, reason: selected.reason };
                    const composite = await encode(options);
                    const comparison = await createImageBitmap(composite.image);
                    const inspection = new OffscreenCanvas(comparison.width, comparison.height);
                    const pixels = inspection.getContext('2d'); pixels.drawImage(comparison, 0, 0);
                    const data = pixels.getImageData(0, 0, comparison.width, comparison.height).data;
                    let red = 0;
                    for (let i = 0; i < data.length; i += 4) if (data[i] > 180 && data[i+1] < 80) red++;
                    if (red < 1000 || comparison.height <= 540) throw new Error('historical popup not visible in comparison');
                    comparison.close(); delete options.context;
                    return { jpegBytes: first.image.size, staticSkipped: same.image === null,
                      resized: true, refreshed: true, forced: true, changed: true, focus: true,
                      transientEvidence: true, originalDetail: true };
                  };
                  try { return { background: await verify(background), fallback: await verify(fallback) }; }
                  finally { worker.terminate(); }
                }""")
                for mode in ('background', 'fallback'):
                    assert result[mode]['jpegBytes'] > 0
                    assert all(value is True for key, value in result[mode].items() if key != 'jpegBytes')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

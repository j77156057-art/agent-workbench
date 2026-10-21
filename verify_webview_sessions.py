"""Real hidden WebView2 windows: session isolation, reload and MPA navigation.

Run against verify_scene_canvas.py --serve 8011, never against a user workspace.
No synthetic claims are counted as a real GPU or engine editor test.
"""
import os
import json
import tempfile
import time
import traceback
import webview


def main():
    base = os.getenv('DOCMIND_UI_TEST_URL', 'http://127.0.0.1:8011')
    result = {'ok': False, 'runtime': 'edgechromium', 'checks': []}
    first = webview.create_window('Agent Workbench session acceptance A', base + '/workbench', hidden=True)
    second = webview.create_window('Agent Workbench session acceptance B', base + '/', hidden=True)

    def session(win, different=None):
        end = time.monotonic() + 25
        while time.monotonic() < end:
            try:
                value = win.evaluate_js('window.DocMindSession && window.DocMindSession.get()')
                if value and value != different:
                    return value
            except Exception:
                pass
            time.sleep(.05)
        raise AssertionError('WebView2 page failed to acquire a session')

    def check():
        try:
            a, b = session(first), session(second)
            assert a != b
            result['checks'].append('independent_windows')
            # Simulate copied storage, retaining the same real WebView2 profile.
            # First's lock must remain held while B leaves its current document.
            second.evaluate_js('sessionStorage.setItem("docmind_session_id", ' + json.dumps(a) + ')')
            second.load_url(base + '/workbench')
            new_b = session(second, b)
            assert new_b != a and session(first) == a
            result['checks'].append('copied_storage_preserves_original')
            first.load_url(base + '/')
            time.sleep(.5)
            assert session(first) == a
            result['checks'].append('question_workbench_navigation')
            first.evaluate_js('location.reload()')
            time.sleep(.5)
            assert session(first) == a
            result['checks'].append('refresh_preserves_session')
            result['user_agent'] = first.evaluate_js('navigator.userAgent')
            result['ok'] = True
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            second.destroy()
            first.destroy()

    with tempfile.TemporaryDirectory(prefix='docmind-webview-acceptance-') as storage:
        webview.start(check, gui='edgechromium', private_mode=True, storage_path=storage)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

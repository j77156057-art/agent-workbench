import types
import unittest
from unittest.mock import Mock, patch
import desktop


class Event:
    def __init__(self): self.handlers = []
    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class HostHandleTests(unittest.TestCase):
    def window(self, native):
        return types.SimpleNamespace(native=native, events=types.SimpleNamespace(loaded=Event()))

    def test_hidden_native_handle_does_not_depend_on_title_enumeration(self):
        win = self.window(types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 1234)))
        with patch('webview.create_window', return_value=win), \
             patch('desktop_bridge.is_window', return_value=True), \
             patch('desktop_bridge.find_host') as search, \
             patch('desktop_bridge.set_host') as register, \
             patch('desktop.urllib.request.urlopen', return_value=Mock()):
            desktop.build_host_window('http://127.0.0.1:8024', project_id='test')
            win.events.loaded.handlers[0]()
            register.assert_called_once_with(1234, 'test')
            search.assert_not_called()

    def test_legacy_backend_retains_title_fallback(self):
        win = self.window(None)
        with patch('webview.create_window', return_value=win), \
             patch('desktop_bridge.find_host', return_value=(5678, 'Agent Workbench')) as search, \
             patch('desktop_bridge.set_host') as register, \
             patch('desktop.urllib.request.urlopen', return_value=Mock()):
            desktop.build_host_window('http://127.0.0.1:8024')
            win.events.loaded.handlers[0]()
            register.assert_called_once_with(5678, '')
            search.assert_called_once()

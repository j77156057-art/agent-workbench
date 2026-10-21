import asyncio
import email.message
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import api
import config
import projects
import secrets_store
import voice
import voice_dialogue


class _FakeVoiceLlm:
    def __init__(self):
        self.messages = None

    def chat(self, messages, stream=False, temperature=0.2):
        self.messages = messages
        return "主 Agent 还在执行，我已记下你的反馈。"


class VoiceBridgeTests(unittest.TestCase):
    def test_unconfigured_stt_explicitly_returns_browser_fallback(self):
        with patch.dict("os.environ", {
            "DOCMIND_STT_URL": "", "DOCMIND_STT_API_KEY": "",
        }, clear=False):
            self.assertFalse(voice.configured("STT"))
            result = voice.transcribe(b"audio", "voice.webm")
            self.assertFalse(result["ok"])
            self.assertEqual(result["fallback"], "browser")

    def test_companion_has_no_tool_context_and_keeps_reply_short(self):
        llm = _FakeVoiceLlm()
        result = voice_dialogue.reply("这里不对，请记下来", "主 Agent 已完成第一版", llm=llm)
        self.assertIn("已记下", result)
        self.assertEqual(len(llm.messages), 2)
        self.assertIn("不能修改文件", llm.messages[0]["content"])
        self.assertIn("这里不对", llm.messages[1]["content"])

    def test_voice_status_and_dialogue_api(self):
        import api
        from starlette.testclient import TestClient
        fake = lambda text, main_result="": "收到：" + text
        with patch("voice_dialogue.reply", side_effect=fake):
            with TestClient(api.app) as client:
                status = client.get("/api/voice/status").json()
                self.assertTrue(status["ok"])
                response = client.post("/api/voice/dialogue", json={"text": "这里不对", "main_result": "执行中"})
                self.assertTrue(response.json()["ok"])
                self.assertIn("这里不对", response.json()["text"])


class _FakeResponse:
    def __init__(self, body=b"", content_type="application/json"):
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VoiceBridgeEndpointTests(unittest.TestCase):
    """显式传入的服务商配置必须真正进入请求，而不是被环境变量盖掉。"""

    _ENV = (
        "DOCMIND_STT_URL", "DOCMIND_STT_API_KEY", "DOCMIND_STT_MODEL",
        "DOCMIND_TTS_URL", "DOCMIND_TTS_API_KEY", "DOCMIND_TTS_MODEL",
        "DOCMIND_TTS_VOICE",
    )

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self._ENV}
        for key in self._ENV:
            os.environ[key] = ""

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_base_url_is_completed_to_audio_endpoint(self):
        self.assertEqual(
            voice._endpoint("https://api.openai.com/v1", "/audio/transcriptions"),
            "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(
            voice._endpoint("https://api.openai.com/v1/", "/audio/speech"),
            "https://api.openai.com/v1/audio/speech")
        self.assertEqual(
            voice._endpoint("https://host/compatible-mode/v1", "/audio/speech"),
            "https://host/compatible-mode/v1/audio/speech")
        self.assertEqual(
            voice._endpoint("https://host/v1/audio/speech", "/audio/speech"),
            "https://host/v1/audio/speech")

    def test_transcribe_uses_explicit_configuration(self):
        response = _FakeResponse(json.dumps({"text": "你好"}).encode())
        with patch("urllib.request.urlopen", return_value=response) as opener:
            result = voice.transcribe(b"audio-bytes", "voice.webm", "zh",
                                      url="https://api.example.com/v1",
                                      key="sk-stt", model="whisper-x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "你好")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.example.com/v1/audio/transcriptions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-stt")
        self.assertIn(b'name="model"\r\n\r\nwhisper-x', request.data)
        self.assertIn(b'name="language"\r\n\r\nzh', request.data)

    def test_speech_uses_explicit_configuration(self):
        response = _FakeResponse(b"ID3audio", "audio/mpeg")
        with patch("urllib.request.urlopen", return_value=response) as opener:
            audio, mime = voice.speech_request(
                "你好", "nova", url="https://api.example.com/v1",
                key="sk-tts", model="tts-x")
        self.assertEqual(audio, b"ID3audio")
        self.assertEqual(mime, "audio/mpeg")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/audio/speech")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-tts")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "tts-x")
        self.assertEqual(payload["voice"], "nova")
        self.assertEqual(payload["input"], "你好")


class VoiceConfigPersistenceTests(unittest.TestCase):
    """语音模型选择跨进程恢复；状态文件里不能出现密钥。"""

    _VOICE_ENV = (
        "DOCMIND_STT_URL", "DOCMIND_STT_API_KEY", "DOCMIND_STT_MODEL",
        "DOCMIND_TTS_URL", "DOCMIND_TTS_API_KEY", "DOCMIND_TTS_MODEL",
        "DOCMIND_TTS_VOICE", "DOCMIND_OMNI_MODEL", "DOCMIND_OMNI_VOICE",
        "DOCMIND_OMNI_TRANSCRIPTION_MODEL",
    )

    def setUp(self):
        self._old_state_root = config.STATE_ROOT
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_voice = {key: dict(value) for key, value in config._VOICE_CONFIG.items()}
        self._saved_env = {key: os.environ.get(key) for key in self._VOICE_ENV}
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_voice_config_")
        self._state_root = os.path.join(self._tmp.name, "state")
        os.makedirs(self._state_root)
        config.STATE_ROOT = self._state_root
        config.STATE_FILE = os.path.join(self._state_root, ".docmind_state.json")
        config._RUNTIME.clear()
        config._VOICE_CONFIG.clear()
        for key in self._VOICE_ENV:
            os.environ[key] = ""

    def tearDown(self):
        config.STATE_ROOT = self._old_state_root
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        config._VOICE_CONFIG.clear()
        config._VOICE_CONFIG.update(self._old_voice)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_voice_choices_survive_restart(self):
        config.set_voice_config("stt", provider="openai",
                                url="https://api.openai.com/v1",
                                model="gpt-4o-transcribe")
        config.set_voice_config("tts", provider="siliconflow",
                                url="https://api.siliconflow.cn/v1",
                                model="FunAudioLLM/CosyVoice2-0.5B", voice="alex")
        config.set_voice_realtime_config(model="qwen3.5-omni-plus-realtime",
                                         voice="Ryan",
                                         transcription_model="gummy-realtime-v1")
        config._VOICE_CONFIG.clear()
        config._RUNTIME.clear()
        config._apply_persisted_state()

        self.assertEqual(config.get_voice_config("stt")["model"], "gpt-4o-transcribe")
        self.assertEqual(config.get_voice_config("tts")["voice"], "alex")
        realtime = config.get_voice_realtime_config()
        self.assertEqual(realtime["omni_model"], "qwen3.5-omni-plus-realtime")
        self.assertEqual(realtime["omni_voice"], "Ryan")
        self.assertEqual(realtime["omni_transcription_model"], "gummy-realtime-v1")
        with open(config.STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertNotIn("api_key", json.dumps(state))

    def test_invalid_url_rejected_without_mutating_state(self):
        with self.assertRaises(ValueError):
            config.set_voice_config("stt", provider="custom",
                                    url="ftp://host/v1", model="m")
        self.assertEqual(config.get_voice_config("stt")["provider"], "env")
        self.assertEqual(config.get_voice_config("stt")["url"], "")

    def test_realtime_override_beats_env_and_can_be_cleared(self):
        os.environ["DOCMIND_OMNI_MODEL"] = "env-model"
        config.set_voice_realtime_config(model="ui-model")
        self.assertEqual(config.get_voice_realtime_config()["omni_model"], "ui-model")
        config.set_voice_realtime_config(model="")
        self.assertEqual(config.get_voice_realtime_config()["omni_model"], "env-model")


class VoiceConfigApiTests(unittest.TestCase):
    """POST /api/voice/config：先校验后落盘、密钥按服务商分槽且永不回显。"""

    _ENV = (
        "DOCMIND_STT_URL", "DOCMIND_STT_API_KEY", "DOCMIND_STT_MODEL",
        "DOCMIND_TTS_URL", "DOCMIND_TTS_API_KEY", "DOCMIND_TTS_MODEL",
        "DOCMIND_TTS_VOICE", "DOCMIND_OMNI_MODEL", "DOCMIND_OMNI_VOICE",
        "DOCMIND_OMNI_TRANSCRIPTION_MODEL", "DOCMIND_OMNI_API_KEY",
        "DOCMIND_REALTIME_PROVIDER", "OPENAI_API_KEY", "SILICONFLOW_API_KEY",
        "DASHSCOPE_API_KEY",
    )

    def setUp(self):
        self._old_state_root = config.STATE_ROOT
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_voice = {key: dict(value) for key, value in config._VOICE_CONFIG.items()}
        self._saved_env = {key: os.environ.get(key) for key in self._ENV}
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_voice_api_")
        self._state_root = os.path.join(self._tmp.name, "state")
        self._project_root = os.path.join(self._tmp.name, "project")
        os.makedirs(self._state_root)
        os.makedirs(self._project_root)
        config.STATE_ROOT = self._state_root
        config.STATE_FILE = os.path.join(self._state_root, ".docmind_state.json")
        config._RUNTIME.clear()
        config._VOICE_CONFIG.clear()
        for key in self._ENV:
            os.environ[key] = ""
        pid = projects.ensure_project(self._project_root)
        self.assertTrue(projects.set_current(pid))

    def tearDown(self):
        config.STATE_ROOT = self._old_state_root
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        config._VOICE_CONFIG.clear()
        config._VOICE_CONFIG.update(self._old_voice)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _post(self, **payload):
        return asyncio.run(api.save_voice_config_ep(api.VoiceConfigReq(**payload)))

    @staticmethod
    def _body(response):
        return json.loads(response.body.decode("utf-8"))

    def test_save_and_read_roundtrip_never_exposes_key(self):
        resp = self._post(
            stt=api.VoiceServiceReq(provider="openai",
                                    url="https://api.openai.com/v1",
                                    model="whisper-1", api_key="sk-voice-secret"),
            tts=api.VoiceServiceReq(provider="siliconflow",
                                    url="https://api.siliconflow.cn/v1",
                                    model="FunAudioLLM/CosyVoice2-0.5B",
                                    voice="alex", api_key="sk-tts-secret"),
            realtime=api.VoiceRealtimeReq(model="qwen3.5-omni-plus-realtime",
                                          voice="Ryan",
                                          transcription_model="gummy-realtime-v1"),
        )
        self.assertTrue(resp["ok"])
        serialized = json.dumps(resp)
        self.assertNotIn("sk-voice-secret", serialized)
        self.assertNotIn("sk-tts-secret", serialized)
        self.assertNotIn("api_key", resp["stt"])
        self.assertTrue(resp["stt"]["has_key"])
        self.assertEqual(resp["stt"]["effective_model"], "whisper-1")
        self.assertEqual(resp["tts"]["effective_voice"], "alex")
        self.assertEqual(resp["realtime"]["effective_voice"], "Ryan")

        self.assertEqual(
            secrets_store.load(self._project_root, "voice:stt:openai"),
            "sk-voice-secret")
        self.assertEqual(
            secrets_store.load(self._project_root, "voice:tts:siliconflow"),
            "sk-tts-secret")

        got = asyncio.run(api.voice_config_ep())
        self.assertNotIn("api_key", got["tts"])
        self.assertTrue(got["tts"]["has_key"])
        status = asyncio.run(api.voice_status_ep())
        self.assertTrue(status["stt_configured"])
        self.assertTrue(status["tts_configured"])
        self.assertEqual(status["stt_model"], "whisper-1")
        self.assertEqual(status["tts_voice"], "alex")

        with open(config.STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertNotIn("sk-voice-secret", json.dumps(state))
        self.assertNotIn("api_key", json.dumps(state["voice_stt"]))

    def test_invalid_urls_rejected_before_any_write(self):
        bad = self._post(stt=api.VoiceServiceReq(provider="custom",
                                                 url="ftp://host/v1", model="m"))
        self.assertEqual(bad.status_code, 400)
        env = self._post(stt=api.VoiceServiceReq(provider="env",
                                                 url="https://host/v1", model="m"))
        self.assertEqual(env.status_code, 400)
        self.assertEqual(config.get_voice_config("stt")["provider"], "env")
        self.assertEqual(config.get_voice_config("stt")["url"], "")

    def test_switching_back_to_env_clears_runtime_key(self):
        self._post(stt=api.VoiceServiceReq(provider="openai",
                                           url="https://api.openai.com/v1",
                                           model="whisper-1", api_key="sk-temp"))
        self.assertEqual(config.get_runtime("voice_stt_api_key"), "sk-temp")
        resp = self._post(stt=api.VoiceServiceReq(provider="env"))
        self.assertTrue(resp["ok"])
        self.assertIsNone(config.get_runtime("voice_stt_api_key"))
        self.assertIsNone(config.get_runtime("voice_stt_api_key_provider"))
        self.assertEqual(resp["stt"]["provider"], "env")
        self.assertFalse(resp["stt"]["configured"])
        # 切回同一服务商仍能取回原来的项目密钥
        self.assertEqual(
            secrets_store.load(self._project_root, "voice:stt:openai"), "sk-temp")

    def test_key_is_scoped_to_its_provider(self):
        self._post(stt=api.VoiceServiceReq(provider="openai",
                                           url="https://api.openai.com/v1",
                                           model="whisper-1", api_key="sk-openai"))
        resp = self._post(stt=api.VoiceServiceReq(
            provider="siliconflow", url="https://api.siliconflow.cn/v1",
            model="FunAudioLLM/SenseVoiceSmall"))
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["stt"]["has_key"],
                         "切服务商时不能把上一家的 Key 带过去")

    def test_cloud_preset_without_key_falls_back_to_browser(self):
        """有地址没 Key 的云端预设等于不可用：configured 必须为 False。

        否则 /api/voice/status 会告诉前端「服务端可用」，前端就不走浏览器
        回退，最终带着空 Authorization 去撞厂商 401。
        """
        resp = self._post(tts=api.VoiceServiceReq(
            provider="openai", url="https://api.openai.com/v1",
            model="gpt-4o-mini-tts", voice="alloy"))
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["tts"]["has_key"])
        self.assertFalse(resp["tts"]["configured"])
        status = asyncio.run(api.voice_status_ep())
        self.assertFalse(status["tts_configured"])
        # custom 端点可能是无 Key 的本地服务，仍按地址判断
        custom = self._post(tts=api.VoiceServiceReq(
            provider="custom", url="http://127.0.0.1:9999/v1",
            model="local-tts", voice="default"))
        self.assertTrue(custom["ok"])
        self.assertTrue(custom["tts"]["configured"])


if __name__ == "__main__":
    unittest.main()

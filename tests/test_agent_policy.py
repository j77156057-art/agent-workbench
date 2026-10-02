import json, tempfile, unittest
from pathlib import Path
from agent_policy import route_for, record_permission, SELF_ROOT, create_external_approval, decide_approval, approval_allows, redact_for_cloud
import secrets_store
import project_state

class AgentPolicyTests(unittest.TestCase):
    def test_local_first_and_cloud_threshold(self):
        self.assertEqual(route_for('解释这个函数')['route'], 'local')
        self.assertEqual(route_for('复杂架构重构并发性能', ['a']*16)['route'], 'cloud')
    def test_self_project_denied_and_external_audited(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(record_permission(str(SELF_ROOT/'x.txt'), root, True, True)['allowed'])
            ext = Path(root).parent/'external-docmind-policy-test.txt'
            self.assertFalse(record_permission(str(ext), root, True, False)['recorded'])
            ok = record_permission(str(ext), root, True, True)
            self.assertTrue(ok['recorded'])
            self.assertTrue(Path(project_state.path(root, 'permissions.jsonl')).exists())
            self.assertFalse((Path(root)/'.docmind_permissions.jsonl').exists())

    def test_secret_roundtrip_and_revoke(self):
        with tempfile.TemporaryDirectory() as root:
            value='sk-test-secret-123'
            self.assertTrue(secrets_store.save(root,'deepseek',value)['ok'])
            raw=Path(project_state.path(root, 'secrets.json')).read_text(encoding='utf-8')
            self.assertFalse((Path(root)/'.docmind_secrets.json').exists())
            self.assertNotIn(value,raw)
            self.assertEqual(secrets_store.load(root,'deepseek'),value)
            self.assertIn('deepseek',secrets_store.providers(root))
            self.assertTrue(secrets_store.remove(root,'deepseek')['removed'])
            self.assertEqual(secrets_store.load(root,'deepseek'),'')

    def test_external_approval_binding(self):
        with tempfile.TemporaryDirectory() as root:
            p=str(Path(root).parent/'approved-file.txt')
            row=create_external_approval(root,[p],'change')
            self.assertFalse(approval_allows(root,row['id'],p))
            decide_approval(root,row['id'],'approved')
            self.assertTrue(approval_allows(root,row['id'],p))

    def test_cloud_redaction(self):
        out=redact_for_cloud('api_key=sk-secret password: hello token=abc123 normal text')
        self.assertNotIn('sk-secret',out); self.assertNotIn('hello',out); self.assertIn('[REDACTED]',out)

    def test_cloud_redaction_handles_quoted_json_credentials(self):
        payload = '{"auth":{"api_key":"abc123secret","password":"my pass",' \
                  '"access_token":"token-value","keep":"visible"}}'
        out = redact_for_cloud(payload)
        parsed = json.loads(out)
        self.assertEqual(parsed['auth']['api_key'], '[REDACTED]')
        self.assertEqual(parsed['auth']['password'], '[REDACTED]')
        self.assertEqual(parsed['auth']['access_token'], '[REDACTED]')
        self.assertEqual(parsed['auth']['keep'], 'visible')

if __name__ == '__main__': unittest.main()

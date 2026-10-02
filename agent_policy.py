from pathlib import Path
import json
from datetime import datetime
import os
import re
import difflib
import project_state
SELF_ROOT = Path(__file__).resolve().parent
def route_for(prompt, files=None, requested='auto'):
    text=str(prompt or '').lower(); score=min(5,len(text)//600)+ (2 if any(x in text for x in ('架构','重构','并发','性能','复杂','blueprint')) else 0)+min(3,len(files or [])//8)
    route=requested if requested in ('local','cloud') else ('cloud' if score>=3 else 'local')
    auto_enabled=os.getenv('AGENT_AUTO_CLOUD','0').lower() in ('1','true','yes','on')
    return {'ok':True,'route':route,'complexity':score,'reason':'复杂度达到升级阈值' if route=='cloud' else '本地模型可处理','auto_cloud_enabled':auto_enabled}

def routing_status():
    return {'auto_cloud_enabled': os.getenv('AGENT_AUTO_CLOUD','0').lower() in ('1','true','yes','on'), 'local_first': True, 'cloud_requires_key': True}

_SECRET_RX = re.compile(
    r'''(?i)(?P<prefix>["']?(?:api[_-]?key|access[_-]?token|token|password|passwd|'''
    r'''client[_-]?secret|secret|private[_-]?key|authorization)["']?\s*[:=]\s*)'''
    r'''(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)'''
)


def _redact_secret(match):
    value = match.group("value")
    quote = value[0] if value[:1] in {"'", '"'} else ""
    return match.group("prefix") + quote + "[REDACTED]" + quote


def redact_for_cloud(text, limit=24000):
    """最小化云端上下文：脱敏常见凭据并限制长度。"""
    value = _SECRET_RX.sub(_redact_secret, str(text or ""))
    value=re.sub(r'(?i)\b(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [^-]+-----.*?-----END [^-]+-----)', '[REDACTED]', value, flags=re.S)
    return value[:max(1000,int(limit))]
def permission_check(path, project_root, allow_external=False):
    p=Path(path).resolve(); selfroot=SELF_ROOT; root=Path(project_root).resolve()
    if p==selfroot or selfroot in p.parents: return {'ok':False,'allowed':False,'reason':'禁止修改 Agent 自身项目'}
    if p==root or root in p.parents: return {'ok':True,'allowed':True,'scope':'project'}
    return {'ok':bool(allow_external),'allowed':bool(allow_external),'scope':'external' if allow_external else None,'requires_approval':bool(allow_external)}

def record_permission(path, project_root, allow_external=False, approved=False):
    result=permission_check(path, project_root, allow_external)
    if not result.get('allowed') or (result.get('scope')=='external' and not approved):
        return {**result, 'recorded': False}
    log=Path(project_state.path(project_root, 'permissions.jsonl', legacy='.docmind_permissions.jsonl'))
    row={'path':str(Path(path).resolve()),'scope':result.get('scope'),'approved':bool(approved),'created_at':datetime.now().isoformat(timespec='seconds')}
    with log.open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return {**result,'recorded':True,'audit':row}

def approval_allows(root, approval_id, path):
    if not approval_id: return False
    for row in list_approvals(root):
        if row.get('id') == approval_id and row.get('status') == 'approved':
            target=str(Path(path).resolve())
            return any(target == str(Path(p).resolve()) for p in row.get('paths', []))
    return False

def apply_approved_external(root, approval_id, path, content):
    if not approval_allows(root, approval_id, path): return {'ok':False,'error':'审批不存在、未批准或路径不匹配'}
    target=Path(path).resolve()
    if not target.is_file(): return {'ok':False,'error':'外部文件不存在，仅允许修改已有文件'}
    if not isinstance(content,str) or len(content.encode())>512*1024: return {'ok':False,'error':'内容无效或超过 512KB'}
    backup=target.with_name(target.name+'.docmind.bak'); backup.write_bytes(target.read_bytes()); target.write_text(content,encoding='utf-8',newline='')
    result=record_permission(str(target),root,True,True); result.update({'written':True,'backup':str(backup)}); return result

def approval_file(root): return Path(project_state.path(root, 'external_approvals.jsonl', legacy='.docmind_external_approvals.jsonl'))
def create_external_approval(root, paths, summary='', diff='', before='', after=''):
    import uuid
    if not diff and (before or after): diff=''.join(difflib.unified_diff(str(before).splitlines(True),str(after).splitlines(True),fromfile='before',tofile='after'))
    row={'id':'APR-'+uuid.uuid4().hex[:12],'paths':[str(Path(p).resolve()) for p in paths], 'summary':str(summary)[:2000], 'diff':str(diff)[:20000], 'status':'pending','created_at':datetime.now().isoformat(timespec='seconds')}
    primary=approval_file(root)
    with primary.open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return row
def list_approvals(root):
    try: return [json.loads(x) for x in approval_file(root).read_text(encoding='utf-8').splitlines() if x.strip()]
    except Exception: return []
def decide_approval(root, aid, status):
    rows=list_approvals(root); found=None
    for row in rows:
        if row.get('id')==aid: row['status']=status; row['decided_at']=datetime.now().isoformat(timespec='seconds'); found=row
    if found:
        with approval_file(root).open('w',encoding='utf-8') as f:
            for row in rows: f.write(json.dumps(row,ensure_ascii=False)+'\n')
        return found
    return None

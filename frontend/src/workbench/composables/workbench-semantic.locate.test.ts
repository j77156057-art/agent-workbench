// 验证 runLocate 失败时显式记录错误、清掉旧结果、清掉高亮。

interface FakeLocateResp {
  files: { path: string; line: number | null }[];
}

let locateImpl: ((q: string) => Promise<FakeLocateResp>) =
  async (_q) => ({ files: [{ path: 'a.py', line: 1 }] });

const state = {
  query: '',
  loading: false,
  paths: [] as string[],
  result: null as FakeLocateResp | null,
  error: '',
  calls: 0,
};

async function runLocate(q: string): Promise<void> {
  state.query = q;
  if (!q.trim()) {
    state.query = '';
    state.paths = [];
    state.result = null;
    state.error = '';
    return;
  }
  state.loading = true;
  state.error = '';
  try {
    state.calls++;
    const r = await locateImpl(q);
    state.result = r;
    state.paths = (r.files || []).map((f) => f.path);
  } catch (e) {
    state.result = null;
    state.paths = [];
    state.error = (e as { message?: string })?.message
      || '定位失败，请检查后端服务是否可用';
  } finally {
    state.loading = false;
  }
}

function assert(name: string, cond: boolean, info = ''): void {
  if (!cond) throw new Error(`断言失败：${name}（${info}）`);
}

async function main(): Promise<void> {
  // 1) 成功：result/paths 设置
  await runLocate('foo');
  assert('case-1 result set', state.result !== null);
  assert('case-1 paths set', state.paths.length === 1);
  assert('case-1 loading false', state.loading === false);
  assert('case-1 error empty', state.error === '');

  // 2) 失败：清掉旧结果 + 设置 error + loading 仍为 false
  locateImpl = async () => { throw new Error('定位接口 500'); };
  await runLocate('foo');
  assert('case-2 result cleared', state.result === null);
  assert('case-2 paths cleared', state.paths.length === 0);
  assert('case-2 loading false', state.loading === false);
  assert('case-2 error set', state.error.includes('定位接口 500'),
         `expected error message, got "${state.error}"`);

  // 3) 重试：error 必须清空
  locateImpl = async (_q) => ({ files: [{ path: 'b.py', line: 2 }] });
  await runLocate('bar');
  assert('case-3 result refreshed', state.result !== null);
  assert('case-3 error cleared on retry', state.error === '',
         `error should be cleared, got "${state.error}"`);

  // 4) 空 query：清掉所有
  await runLocate('   ');
  assert('case-4 query cleared', state.query === '');
  assert('case-4 result null', state.result === null);
  assert('case-4 paths empty', state.paths.length === 0);
  assert('case-4 error empty', state.error === '');
}

main().catch((e) => { console.error(e); process.exit(1); });

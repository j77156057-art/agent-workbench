// 验证 openSse 的协议畸形计数与回调：5 条 malformed data 行
// 触发一次 onProtocolError（不打断流）。


interface CallRecord {
  count: number;
  countAtReport: number;
  lastPayload: string;
}

let parser: ((s: string) => unknown) = () => { throw new Error('default parse fail'); };
let onEventCalls = 0;
let onErrorCalls = 0;
const report: CallRecord = { count: 0, countAtReport: 0, lastPayload: '' };

// 模拟 openSse 内部处理 chunk 的核心循环（剥离 fetch/reader/TextDecoder
// 之外，仅留协议解析部分）
function handleChunk(chunk: string): void {
  for (const raw of chunk.split('\n')) {
    const line = raw.trimStart();
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      const event = parser(payload);
      onEventCalls++;
      // 模拟正常事件被消费
      void event;
    } catch {
      report.count++;
      report.lastPayload = payload;
      if (report.count === 5) {
        onErrorCalls++;
        report.countAtReport = report.count;
      }
    }
  }
}

function assert(name: string, cond: boolean, info = ''): void {
  if (!cond) throw new Error(`断言失败：${name}（${info}）`);
}

function main(): void {
  // 1) 5 条 malformed 行 → 触发一次 onProtocolError
  parser = () => { throw new Error('not json'); };
  handleChunk('data: {not json}\ndata: x\ndata: y\ndata: z\ndata: w');
  assert('case-1 触发 onError', onErrorCalls === 1, `got ${onErrorCalls}`);
  assert('case-1 触发时 count=5', report.countAtReport === 5);
  assert('case-1 第 5 条 payload 记录', report.lastPayload === 'w');
  assert('case-1 onEvent 未被调', onEventCalls === 0);

  // 2) 之后再加 malformed → 不再触发 onError（避免每次都报）
  handleChunk('data: a\ndata: b\ndata: c\ndata: d\ndata: e\ndata: f\ndata: g');
  assert('case-2 onError 仍只 1 次', onErrorCalls === 1, `got ${onErrorCalls}`);
  assert('case-2 count 累加到 12', report.count === 12, `got ${report.count}`);

  // 3) 混着正常事件：malformed 不应影响正常事件被消费
  onEventCalls = 0;
  report.count = 0;
  report.countAtReport = 0;
  onErrorCalls = 0;
  let call = 0;
  parser = (s) => {
    call++;
    if (call % 3 === 0) {
      // 每 3 条让 1 条失败：实际累计 2 个 malformed（2/3 在 5 之前）
      throw new Error('boom');
    }
    return { kind: 'ok', data: s };
  };
  // 9 条数据 → 6 条正常 + 3 条 malformed（首次报 onError 在第 5 个 malformed）
  const inputs = Array.from({ length: 9 }, (_, i) => `data: e${i}`).join('\n');
  handleChunk(inputs);
  // 实际：9 条 → 3 个 malformed（1/3/5/7/9，c % 3 === 0 → c=3,6,9）= 3 个
  // 不对：c 每次自增，每 3 条失败一次；c=3,6,9 失败 → 3 个 malformed
  // 5 个阈值：malformed 数量 3 < 5，不应触发
  assert('case-3 onError 未触发（仅 3 malformed）',
         onErrorCalls === 0, `got ${onErrorCalls}`);
  assert('case-3 onEvent 6 次', onEventCalls === 6, `got ${onEventCalls}`);

  // 4) 累计到 5：再补 5 条 100% malformed
  parser = () => { throw new Error('always fail'); };
  handleChunk('data: a\ndata: b\ndata: c\ndata: d\ndata: e\ndata: f\ndata: g');
  // 之前累计 3，再加 7 → 10；触发时机：累计 5 时
  assert('case-4 onError 触发 1 次', onErrorCalls === 1, `got ${onErrorCalls}`);
  assert('case-4 报告时 count=5', report.countAtReport === 5);
}

main();

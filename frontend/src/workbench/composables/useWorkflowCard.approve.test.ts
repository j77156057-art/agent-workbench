// 验证 useWorkflowCard.onApprove 在后端「无 workflow 返回」时强制 hydrate
// 真实状态，避免卡片永远卡在乐观 `executing`。
//
// 用纯 TypeScript 复刻 onApprove 的提交逻辑，断言：
// 1) wasWaiting + approve 返回无 workflow → 触发 hydrate
// 2) planned + execute 返回 awaiting_approval + approve 返回无 workflow → 触发 hydrate
// 3) planned + execute 返回无 workflow → 触发 hydrate
// 4) happy path（带 workflow）→ 仍然走原 setState，不重复 hydrate

type Result = { ok?: boolean; workflow?: any | null; error?: string } | null | undefined;

interface Env {
  approveCalls: number;
  executeCalls: number;
  hydrateCalls: number;
  lastState: any;
}

async function onApproveInner(
  env: Env,
  opts: {
    wasWaiting: boolean;
    approveResult: Result;
    executeResult: Result;
    hydrateResult: any;
  },
): Promise<void> {
  if (opts.wasWaiting) {
    const r = opts.approveResult;
    if (!r?.workflow) {
      env.hydrateCalls++;
      env.lastState = await Promise.resolve(opts.hydrateResult);
      return;
    }
    return;
  }
  const r = opts.executeResult;
  if (r?.workflow && r.workflow.status === 'awaiting_approval') {
    const continued = opts.approveResult;
    if (!continued?.workflow) {
      env.hydrateCalls++;
      env.lastState = await Promise.resolve(opts.hydrateResult);
      return;
    }
    return;
  }
  if (!r?.workflow) {
    env.hydrateCalls++;
    env.lastState = await Promise.resolve(opts.hydrateResult);
    return;
  }
  return;
}

function assert(name: string, cond: boolean, info = ''): void {
  if (!cond) throw new Error(`断言失败：${name}（${info}）`);
}

function main(): void {
  // 1) wasWaiting + approve 无 workflow → hydrate 必须被调
  {
    const env: Env = { approveCalls: 0, executeCalls: 0, hydrateCalls: 0, lastState: null };
    void onApproveInner(env, {
      wasWaiting: true,
      approveResult: { ok: true, workflow: null },
      executeResult: null,
      hydrateResult: { status: 'executing', events: [{ seq: 1 }] },
    });
    setTimeout(() => {
      assert('case-1 hydrate called', env.hydrateCalls === 1, `got ${env.hydrateCalls}`);
      assert('case-1 state hydrated', env.lastState && env.lastState.status === 'executing');
    }, 0);
  }

  // 2) planned + execute awaiting_approval + approve 无 workflow → hydrate
  setTimeout(() => {
    const env: Env = { approveCalls: 0, executeCalls: 0, hydrateCalls: 0, lastState: null };
    void onApproveInner(env, {
      wasWaiting: false,
      approveResult: { ok: true, workflow: null },
      executeResult: { ok: true, workflow: { status: 'awaiting_approval' } },
      hydrateResult: { status: 'executing' },
    }).then(() => {
      assert('case-2 hydrate called', env.hydrateCalls === 1, `got ${env.hydrateCalls}`);
      assert('case-2 state hydrated', env.lastState && env.lastState.status === 'executing');
    });
  }, 5);

  // 3) planned + execute 无 workflow → hydrate
  setTimeout(() => {
    const env: Env = { approveCalls: 0, executeCalls: 0, hydrateCalls: 0, lastState: null };
    void onApproveInner(env, {
      wasWaiting: false,
      approveResult: { ok: true, workflow: { status: 'planned' } },
      executeResult: { ok: true, workflow: null },
      hydrateResult: { status: 'executing' },
    }).then(() => {
      assert('case-3 hydrate called', env.hydrateCalls === 1, `got ${env.hydrateCalls}`);
    });
  }, 10);

  // 4) happy path 带 workflow → 不调用 hydrate
  setTimeout(() => {
    const env: Env = { approveCalls: 0, executeCalls: 0, hydrateCalls: 0, lastState: null };
    void onApproveInner(env, {
      wasWaiting: true,
      approveResult: { ok: true, workflow: { status: 'planned' } },
      executeResult: null,
      hydrateResult: null,
    }).then(() => {
      assert('case-4 happy path no hydrate', env.hydrateCalls === 0, `got ${env.hydrateCalls}`);
    });
  }, 15);
}

main();

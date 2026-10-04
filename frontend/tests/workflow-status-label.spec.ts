// 「已完成 · 未做独立验证」——只读任务的 `failed` 不该显示成「执行失败」。
//
// ## 为什么要这一层
//
// 实测（`wf-e042489bf0fe`）：四个子代理全 `ok`、十项复核检查全绿、
// `evidence_verified: true`、144 条引用 0 条不过、4133 字结论全部正确交付——
// 而 `status` 是 `failed`。
//
// 根因在 `game_workflow.py:3245`把两个维度压成一个布尔：
//
//     state.review["agent_ok"] = ...                 // 十项检查 + 证据核验
//     state.review["ok"] = agent_ok and project["ok"]  // 后者说的是另一件事
//
// `project.ok=false` 的原话是「试做区没有容器级命令隔离，**不能作为独立测试通过
// 的证据**」——那是**给这次结果标注证据等级**，不是「有东西坏了」。而一次纯只读
// 侦察（`change_count: 0`）本来就没打算跑测试。
//
// 后端已经算好了 `agent_ok`，所以这一层只读它、**零后端改动**——不去动那 17 处
// `status in {completed, failed}` 的状态判断（SSE 关流、恢复/丢弃、经验沉淀、
// 历史列表删除都挂在上面）。
import { describe, expect, it } from 'vitest'
import { STATUS_LABEL, displayStatusOf, statusLabelOf } from '../src/workbench/composables/workflow-format'

/** 实测那次运行的 review 形状（字段与后端 game_workflow.py:3243-3245 一致）。 */
const READ_ONLY_REVIEW = {
  agent_ok: true,
  ok: false,
  checks: {
    has_result: true, no_replacement_chars: true, credentials_absent: true,
    sensitive_terms_without_value: true, tool_failures_below_limit: true,
    status_ok: true, file_evidence_bounded: true, test_evidence_bounded: true,
    tests_ok: true, evidence_verified: true,
  },
  evidence_audit: { ok: true, gate_ok: true, verdict: 'PASS' },
  project: {
    ok: false, status: 'unverified', independent: true, change_count: 0,
    message: '试做区没有容器级命令隔离，不能作为独立测试通过的证据',
  },
}

describe('statusLabelOf', () => {
  it('只读侦察的 failed 应该说「已完成 · 未做独立验证」', () => {
    expect(statusLabelOf({ status: 'failed', review: READ_ONLY_REVIEW }))
      .toBe('已完成 · 未做独立验证')
  })

  it('**真失败**必须照旧说「执行失败」', () => {
    // agent_ok 假 = 十项检查里有没过 —— 那才是失败，绝不能被软化
    expect(statusLabelOf({
      status: 'failed',
      review: { ...READ_ONLY_REVIEW, agent_ok: false },
    })).toBe('执行失败')
  })

  it('两项都真就是普通的已完成', () => {
    expect(statusLabelOf({
      status: 'failed',
      review: { ...READ_ONLY_REVIEW, project: { ...READ_ONLY_REVIEW.project, ok: true } },
    })).toBe('执行失败') // status仍是 failed，只是不该被改写成「已完成」
  })

  it('completed 不受影响', () => {
    expect(statusLabelOf({ status: 'completed' })).toBe('已完成')
  })

  it('缺 review 时退回原 status', () => {
    expect(statusLabelOf({ status: 'failed' })).toBe('执行失败')
    expect(statusLabelOf(null)).toBe('preparing')
  })

  it('未知 status 原样透出，不吞掉', () => {
    expect(statusLabelOf({ status: 'executing' })).toBe('执行中')
    expect(statusLabelOf({ status: '某个新状态' })).toBe('某个新状态')
  })
})

describe('displayStatusOf', () => {
  it('改写只发生在 failed 且 agent_ok 真/project 假时', () => {
    expect(displayStatusOf({ status: 'failed', review: READ_ONLY_REVIEW }))
      .toBe('completed_unverified')
  })

  it('**其它一律不动**——尤其 completed 与 interrupted', () => {
    for (const s of ['completed', 'interrupted', 'executing', 'awaiting_approval']) {
      expect(displayStatusOf({ status: s, review: READ_ONLY_REVIEW })).toBe(s)
    }
  })

  it('agent_ok 假时不改写', () => {
    expect(displayStatusOf({
      status: 'failed', review: { ...READ_ONLY_REVIEW, agent_ok: false },
    })).toBe('failed')
  })

  it('project 也真时不改写', () => {
    expect(displayStatusOf({
      status: 'failed',
      review: { ...READ_ONLY_REVIEW, project: { ...READ_ONLY_REVIEW.project, ok: true } },
    })).toBe('failed')
  })
})

describe('展示态有对应的样式标签', () => {
  it('LABEL 里有 completed_unverified', () => {
    expect(STATUS_LABEL.completed_unverified).toBeTruthy()
  })

  it('它不带「失败」二字', () => {
    // 用户看到「失败」会去查哪里坏了——而实际内容全部正确交付
    expect(STATUS_LABEL.completed_unverified).not.toContain('失败')
  })
})
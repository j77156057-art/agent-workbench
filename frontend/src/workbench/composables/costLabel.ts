/**
 * 费用的显示口径：这个 ¥0.00 到底意味着什么。
 *
 * 三种情况在数字上**完全一样**，可它们要求用户做的事完全不同：
 *   · free    —— 官方标价就是 0：确认过，确实省了钱，什么都不用做；
 *   · local   —— 本地模型：不打 API，本来就不花钱；
 *   · unknown —— **没配置单价**：这个 0 只是「没算」，不是「免费」。
 *
 * 不分开的话，用户看到 ¥0.00 完全无从判断该去配价格还是该换模型——
 * 于是「费用不对」变成一个查不出原因的问题（实测：OpenRouter 的免费模型
 * 就是这样被怀疑成「系统没在算账」的）。
 *
 * 数据来自后端 `pricing.pricing_state()`，前端只负责把状态翻成人话；
 * **不自己猜**（比如拿 cost===0 反推免费），那正是原先的毛病。
 */

export type PricingState = '' | 'free' | 'local' | 'unknown' | 'known'

const LABEL: Record<string, string> = {
  free: '该模型免费',
  local: '本地模型',
  unknown: '未配置单价，费用未计入',
}

export function money(value: number | null | undefined): string {
  return `¥${Number(value || 0).toFixed(4)}`
}

/**
 * 金额 + 这个 0 的含义。有金额时只显示金额；为 0 时才补状态说明。
 * 状态缺失（老数据 / 后端没给）时**退回纯金额**，不要凭空说"免费"。
 */
export function costLabel(cost: number | null | undefined, state?: PricingState): string {
  const amount = money(cost)
  if (Number(cost || 0) > 0) return amount
  const note = state ? LABEL[state] : ''
  return note ? `${amount}（${note}）` : amount
}

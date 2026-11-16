/** 卡片墙 / 流程画布 / 运行台共用的时间格式化（2026-11-17 去重）。
    full=true 出 YYYY-MM-DD HH:mm（卡片墙契约）；默认 zh-CN 月日 时:分。
    空值回退由调用方声明（卡片墙用「未记录」），非法日期原样返回。 */
export function fmtTime(iso?: string, opts: { empty?: string; full?: boolean } = {}): string {
  if (!iso) return opts.empty ?? ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  if (opts.full) {
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
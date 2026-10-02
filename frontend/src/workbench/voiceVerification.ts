/** Consent belongs to one visible clue; silence and ambiguous replies never grant it. */
export function createVoiceVerificationGate() {
  let pending: { id: string; clue: string; at: number; checking: boolean } | null = null
  const expired = (now: number) => !!pending && !pending.checking && now - pending.at > 120_000
  const approve = (id: string, now: number) => {
    if (!pending || pending.id !== id || pending.checking || expired(now)) return false
    pending.checking = true
    return true
  }
  return {
    offer(id: string, clue: string, now: number): boolean {
      if (expired(now)) pending = null
      if (pending || !id || !clue.trim()) return false
      pending = { id, clue: clue.trim(), at: now, checking: false }
      return true
    },
    approve,
    decide(text: string, now: number): 'none' | 'approve' | 'decline' | 'clarify' | 'checking' | 'expired' {
      if (!pending) return 'none'
      if (expired(now)) { pending = null; return 'expired' }
      if (pending.checking) return 'checking'
      const reply = text.trim().replace(/[\s，。！？、,.!?]/g, '')
      if (/^(?:不用|不需要|不要|先不用|暂时不用|算了|取消|不了|不可以|不用核查|不用检查|不要检查|不要核查|先别查|不用了)$/.test(reply)) {
        pending = null
        return 'decline'
      }
      if (/^(?:可以|可以的|好|好的|行|同意|查吧|核查吧|检查吧|查一下|核查一下|检查一下|帮我查一下|帮我核查一下|帮我检查一下|可以核查|可以核查一下|可以检查一下|好核查一下|好的核查一下|好检查一下|好的检查一下)$/.test(reply)) {
        approve(pending.id, now)
        return 'approve'
      }
      return 'clarify'
    },
    matches(id: string): boolean { return pending?.id === id && pending.checking },
    retry(id: string, now: number) {
      if (pending?.id === id) { pending.checking = false; pending.at = now }
    },
    complete(id: string): boolean {
      if (pending?.id !== id || !pending.checking) return false
      pending = null
      return true
    },
    reset() { pending = null },
  }
}

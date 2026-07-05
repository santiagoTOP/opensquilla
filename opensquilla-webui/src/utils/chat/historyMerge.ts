import type { ChatMessage } from '@/types/chat'

// Live-only fields are written onto a row AFTER it is pushed (reasoning seconds
// from the done backfill, routerSettled from the router runtime, interrupted
// from a local Stop) and are absent from a fresh history map. Re-apply them
// when the server snapshot lacks a richer value, keyed strictly by messageId so
// a synthetic-key collision can never graft one turn's state onto another.
export function mergeLiveOnlyFields(prev: ChatMessage, server: ChatMessage): ChatMessage {
  const merged: ChatMessage = { ...server }

  // reasoning: server wins if it measured seconds; else keep the live seconds.
  const serverSeconds = prev.role === 'assistant' ? server.reasoning?.seconds ?? 0 : 0
  if (serverSeconds <= 0 && (prev.reasoning?.seconds ?? 0) > 0) {
    merged.reasoning = prev.reasoning
  }

  // routerSettled is sticky: once a strip has settled it stays settled.
  if (prev.routerSettled) merged.routerSettled = true

  // interrupted: keep the local abort flag until the server persists its own.
  if (server.interrupted === undefined && prev.interrupted) {
    merged.interrupted = prev.interrupted
  }

  return merged
}

// Server-authoritative merge: ordering and membership are exactly the incoming
// (server) window — rows the server dropped (e.g. via compaction) are not kept.
// For each server row that matches a prior row by a REAL messageId, ride the
// prior row's live-only fields along. Rows without a messageId, or with only a
// synthetic fallback key, take the server value verbatim (today's behavior).
export function reconcileHistoryMessages(prev: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (prev.length === 0) return incoming
  const prevById = new Map<string, ChatMessage>()
  for (const msg of prev) {
    if (msg.messageId) prevById.set(msg.messageId, msg)
  }
  if (prevById.size === 0) return incoming
  return incoming.map(server => {
    if (!server.messageId) return server
    const prior = prevById.get(server.messageId)
    return prior ? mergeLiveOnlyFields(prior, server) : server
  })
}

function fallbackMessageKey(msg: ChatMessage): string {
  return `${msg.role}:${msg.ts || ''}:${msg.text || ''}`
}

function lastUserIndex(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'user') return i
  }
  return -1
}

function insertionIndexForLiveTail(
  merged: ChatMessage[],
  previousLastUser: ChatMessage,
): number {
  if (previousLastUser.messageId) {
    const byId = merged.findIndex(msg => msg.messageId === previousLastUser.messageId)
    if (byId >= 0) return byId
  }
  const previousKey = fallbackMessageKey(previousLastUser)
  const byFallback = merged.findIndex(msg => fallbackMessageKey(msg) === previousKey)
  if (byFallback >= 0) return byFallback
  return lastUserIndex(merged)
}

// Running/live history sync is intentionally less server-authoritative than the
// settled reconcile above: a cold transcript snapshot may not yet contain the
// in-flight router strip, tool row, or partial assistant row that replay already
// rebuilt locally. Preserve the local tail after the last user until terminal
// sync makes the transcript authoritative again.
export function reconcileRunningHistoryMessages(
  prev: ChatMessage[],
  incoming: ChatMessage[],
): ChatMessage[] {
  if (prev.length === 0) return incoming
  if (incoming.length === 0) return prev

  const previousLastUserIndex = lastUserIndex(prev)
  if (previousLastUserIndex < 0) return reconcileHistoryMessages(prev, incoming)

  const liveTail = prev.slice(previousLastUserIndex + 1)
  if (liveTail.length === 0) return reconcileHistoryMessages(prev, incoming)

  const merged = reconcileHistoryMessages(prev, incoming)
  const existingIds = new Set(merged.map(msg => msg.messageId).filter(Boolean))
  const existingFallbackKeys = new Set(merged.map(fallbackMessageKey))
  const tailToPreserve = liveTail.filter(msg => {
    if (msg.messageId) return !existingIds.has(msg.messageId)
    return !existingFallbackKeys.has(fallbackMessageKey(msg))
  })
  if (tailToPreserve.length === 0) return merged

  const insertAfter = insertionIndexForLiveTail(merged, prev[previousLastUserIndex])
  if (insertAfter < 0) return [...merged, ...tailToPreserve]
  return [
    ...merged.slice(0, insertAfter + 1),
    ...tailToPreserve,
    ...merged.slice(insertAfter + 1),
  ]
}

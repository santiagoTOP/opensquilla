// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { useVoiceInput } from './useVoiceInput'

// useToasts is a module-level singleton, so this is the same queue the
// composable writes to.
const { toasts } = useToasts()

// Minimal MediaRecorder stand-in: stop() synchronously emits one chunk and
// fires onstop, which is what drives useVoiceInput into transcribeChunks().
class FakeMediaRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(public stream: unknown) {}
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

function stubMedia() {
  ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeMediaRecorder
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop() {} }] })) },
  })
}

async function waitUntil(condition: () => boolean, tries = 40) {
  for (let i = 0; i < tries; i++) {
    if (condition()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

// Start then stop a recording, then wait for the async transcription to settle
// (a toast surfaced or the transcript delivered).
async function runTranscription() {
  const onText = vi.fn()
  const { toggleVoiceInput } = useVoiceInput()
  await toggleVoiceInput(onText)
  await toggleVoiceInput(onText)
  await waitUntil(() => toasts.value.length > 0 || onText.mock.calls.length > 0)
  return onText
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  toasts.value = []
  stubMedia()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useVoiceInput transcription feedback', () => {
  it('surfaces an "enable in settings" toast when transcription is unavailable (503)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: 'audio transcription is disabled', code: 'UNAVAILABLE' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(i18n.global.t('chat.toast.voiceUnavailable'))
    expect(toasts.value.every(t => t.tone === 'danger')).toBe(true)
  })

  it('surfaces a generic failure toast on a provider error (502)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => ({ error: 'ELEVENLABS_API_KEY is not set', code: 'PROVIDER_ERROR' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('surfaces a generic failure toast when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down')
      }),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('inserts the transcript and raises no error toast on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ text: '  hello world  ' }),
      })),
    )

    const onText = await runTranscription()

    expect(onText).toHaveBeenCalledWith('hello world')
    expect(toasts.value).toHaveLength(0)
  })
})

<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import Icon from '@/components/Icon.vue'
import GatewayStatusBlock from '@/components/settings/GatewayStatusBlock.vue'
import { usePlatform, type GatewayStatus } from '@/platform'
import { useConfirm } from '@/composables/useConfirm'
import { useToasts } from '@/composables/useToasts'

// Desktop-only Runtime section of the shared SettingsDialog. The desktop app
// owns its local gateway process, so this surfaces its status/log/restart and
// the "reset saved setup" escape hatch — the controls the old standalone
// DesktopSettingsView carried. Web never renders this (desktopOnly section).
const platform = usePlatform()
const { confirm } = useConfirm()
const { pushToast } = useToasts()

const loading = ref(true)
const busy = ref(false)
const gateway = shallowRef<GatewayStatus | null>(null)

const STATUS_LABELS: Record<string, string> = {
  starting: 'Starting',
  ready: 'Ready',
  stopped: 'Stopped',
  error: 'Error',
}

const statusLabel = computed(() => STATUS_LABELS[gateway.value?.status ?? ''] || 'Unknown')
const gatewayError = computed(() => gateway.value?.error || '')
const url = computed(() => gateway.value?.url || 'No active gateway')
const logAvailable = computed(() => Boolean(gateway.value?.logPath))
const logHint = computed(() => gateway.value?.logPath || 'No local log path')

// This panel only ever mounts on desktop (SettingsDialog gates it behind
// isDesktop), so the capability flags are always true here; gate the buttons on
// the optional methods actually being wired instead.
const canRevealLog = computed(() => Boolean(platform.gateway.revealLog))
const canRestart = computed(() => Boolean(platform.gateway.retryStartup))
const canReset = computed(() => Boolean(platform.settings.resetDesktopSettings))

async function loadStatus() {
  loading.value = true
  try {
    gateway.value = await platform.gateway.getStatus()
  } catch (err) {
    pushToast('Failed to read gateway status: ' + (err instanceof Error ? err.message : String(err)), { tone: 'danger' })
  } finally {
    loading.value = false
  }
}

async function revealLog() {
  if (!platform.gateway.revealLog) return
  try {
    const ok = await platform.gateway.revealLog()
    if (!ok) pushToast('No gateway log to reveal yet.', { tone: 'danger' })
  } catch (err) {
    pushToast('Could not reveal log: ' + (err instanceof Error ? err.message : String(err)), { tone: 'danger' })
  }
}

async function restartGateway() {
  if (!platform.gateway.retryStartup) return
  busy.value = true
  try {
    await platform.gateway.retryStartup()
    pushToast('Restarting the local runtime…')
    await loadStatus()
  } catch (err) {
    pushToast('Restart failed: ' + (err instanceof Error ? err.message : String(err)), { tone: 'danger' })
  } finally {
    busy.value = false
  }
}

async function resetSetup() {
  if (!platform.settings.resetDesktopSettings) return
  const ok = await confirm({
    title: 'Reset saved setup?',
    body: 'This clears the saved desktop credential and generated config. The next launch re-runs first-time setup.',
    primaryLabel: 'Reset',
  })
  if (!ok) return
  busy.value = true
  try {
    await platform.settings.resetDesktopSettings()
    pushToast('Saved setup cleared. Restart the desktop app to re-run setup.')
  } catch (err) {
    pushToast('Reset failed: ' + (err instanceof Error ? err.message : String(err)), { tone: 'danger' })
  } finally {
    busy.value = false
  }
}

onMounted(loadStatus)
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">Runtime</h3>
      <p class="control-section__desc">The desktop app owns the local gateway and serves this Control UI from it.</p>
    </div>

    <div class="runtime-grid">
      <GatewayStatusBlock label="Gateway" :value="loading ? 'Loading…' : statusLabel" :hint="gatewayError || url" />
      <GatewayStatusBlock label="Runtime" value="Local" hint="Desktop-owned process" />
      <GatewayStatusBlock label="Gateway log" :value="logAvailable ? 'Available' : 'Unavailable'" :hint="logHint" />
    </div>

    <div class="runtime-actions">
      <button type="button" class="btn btn--ghost" :disabled="loading || busy" @click="loadStatus">
        <Icon name="refresh" :size="15" />
        <span>Refresh</span>
      </button>
      <button v-if="canRevealLog" type="button" class="btn btn--ghost" :disabled="!logAvailable" @click="revealLog">
        <Icon name="logs" :size="15" />
        <span>Reveal log</span>
      </button>
      <button v-if="canRestart" type="button" class="btn btn--ghost" :disabled="busy" @click="restartGateway">
        <Icon name="refresh" :size="15" />
        <span>Restart runtime</span>
      </button>
    </div>

    <div v-if="canReset" class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">Reset saved setup</span>
        <span class="control-row__desc">Clear the saved credential and generated config, then re-run first-time setup on next launch.</span>
      </div>
      <div class="control-row__control">
        <button type="button" class="btn btn--ghost runtime-reset" :disabled="busy" @click="resetSetup">
          Reset
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.runtime-grid {
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.runtime-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.runtime-reset {
  color: var(--danger);
}
</style>

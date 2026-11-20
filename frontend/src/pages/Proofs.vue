<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const legs = ref([])
const audit = ref(null)
const occId = ref('')
const verdict = ref(null)
const err = ref('')

async function load() {
  proofs.value = await api.proofs()
  legs.value = await api.closeLegs()
  audit.value = await api.auditTwoLeg()
}
async function verify() {
  err.value = ''
  verdict.value = null
  if (!occId.value) return
  try {
    verdict.value = await api.verifyTwoLeg(occId.value.trim())
  } catch (e) {
    err.value = String(e.message || e)
  }
}
onMounted(load)
</script>
<template>
  <div data-plant="30-two-leg-ack-order-lie" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">
      双腿关闭核对：占用转 released 必须挂同运单、同格口的 pickup 与 confirm 两条腿。
    </p>

    <div class="panel" :style="{ borderColor: audit && !audit.ok ? 'var(--over)' : 'var(--line)' }">
      <h2 style="margin:0 0 .5rem;font-size:1rem">一致性审计</h2>
      <p v-if="audit && audit.ok" class="sub" style="margin:0; color:var(--free)">
        全部占用符合双腿栅栏：无单腿释放、无孤儿边。
      </p>
      <template v-else-if="audit">
        <p class="sub" style="margin:0 0 .5rem; color:var(--over)">
          发现 {{ audit.violations.length }} 条违规
        </p>
        <table>
          <thead><tr><th>类型</th><th>占用</th><th>证据</th></tr></thead>
          <tbody>
            <tr v-for="(v, i) in audit.violations" :key="i">
              <td><code>{{ v.kind }}</code></td>
              <td><code>{{ v.occupancy_id || v.occupancy?.id || '—' }}</code></td>
              <td><code>{{ JSON.stringify(v).slice(0, 110) }}</code></td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">单占用核对</h2>
      <div style="display:flex; gap:.5rem; align-items:center; flex-wrap:wrap">
        <input
          v-model="occId"
          placeholder="占用 id，例如 OCC-CAB-A-03-E5"
          style="flex:1; min-width:260px; background:#111827; color:#e8eef6; border:1px solid #334155; border-radius:4px; padding:.45rem"
          @keyup.enter="verify"
        />
        <button @click="verify">核对</button>
      </div>
      <p v-if="err" class="sub" style="color:var(--over); margin:.6rem 0 0">{{ err }}</p>
      <div v-if="verdict" style="margin-top:.75rem">
        <p class="sub" :style="{ color: verdict.verified ? 'var(--free)' : 'var(--over)' }">
          {{ verdict.verified ? '双腿齐备，关闭有效' : '双腿未齐备，关闭无效' }}
          · 状态 {{ verdict.occupancy.status }}
        </p>
        <table>
          <thead><tr><th>检查项</th><th>结果</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in verdict.checks" :key="c.name">
              <td>{{ c.name }}</td>
              <td :style="{ color: c.ok ? 'var(--free)' : 'var(--over)' }">{{ c.ok ? '通过' : '失败' }}</td>
              <td class="sub" style="margin:0">{{ c.detail || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="verdict.leg" style="margin-top:.6rem" class="sub">
          腿：pickup <code>{{ verdict.leg.pickup_id }}</code> ·
          confirm <code>{{ verdict.leg.confirm_id }}</code> ·
          关闭于 <code>{{ verdict.leg.closed_at }}</code>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">双腿关闭边</h2>
      <p v-if="!legs.length" class="sub">暂无关闭边——单腿不会产生边。</p>
      <table v-else>
        <thead>
          <tr><th>占用</th><th>格口</th><th>运单</th><th>pickup</th><th>confirm</th><th>关闭时刻</th></tr>
        </thead>
        <tbody>
          <tr v-for="l in legs" :key="l.id">
            <td><code>{{ l.occupancy_id }}</code></td>
            <td>{{ l.cell_id }}</td>
            <td>{{ l.tracking_no }}</td>
            <td><code>{{ l.pickup_id }}</code> <span class="sub">{{ l.pickup_ts }}</span></td>
            <td><code>{{ l.confirm_id }}</code> <span class="sub">{{ l.confirm_ts }}</span></td>
            <td>{{ l.closed_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用核对（开账 / 关账事件）</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>关闭</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td>{{ p.closed_at || '—' }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

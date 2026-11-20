<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const rows = ref([])
const q = ref([])
const candidates = ref([])
const sweep = ref(null)
const msg = ref('')
async function load() {
  rows.value = await api.occupancies()
  q.value = await api.quarantine()
  candidates.value = await api.closeCandidates()
}
async function runSweep() {
  sweep.value = await api.sweepTwoLeg()
  msg.value = `超时扫描：${sweep.value.expired} 条候选过期，仍留搁置`
  await load()
}
onMounted(load)
</script>
<template>
  <div data-plant="30-two-leg-ack-order-lie" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">
      未配对双腿保持搁置，超时整组过期但不放行；占用账、候选区与搁置共览。
    </p>

    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>开启</th><th>关闭</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td :style="{ color: r.status === 'released' ? 'var(--free)' : 'var(--busy)' }">{{ r.status }}</td>
            <td>{{ r.opened_at }}</td>
            <td>{{ r.closed_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <div style="display:flex; align-items:center; gap:.75rem; flex-wrap:wrap">
        <h2 style="margin:0;font-size:1rem">双腿候选区</h2>
        <button @click="runSweep">跑超时扫描</button>
        <span class="sub">{{ msg }}</span>
      </div>
      <p v-if="!candidates.length" class="sub" style="margin-top:.6rem">暂无候选。</p>
      <table v-else style="margin-top:.6rem">
        <thead><tr><th>id</th><th>占用</th><th>腿</th><th>事件</th><th>状态</th><th>搁置关联</th></tr></thead>
        <tbody>
          <tr v-for="c in candidates" :key="c.id">
            <td>{{ c.id }}</td>
            <td><code>{{ c.occupancy_id }}</code></td>
            <td>{{ c.event_type }}</td>
            <td><code>{{ c.event_id }}</code></td>
            <td>{{ c.status }}<span v-if="c.reason" class="sub"> · {{ c.reason }}</span></td>
            <td><code v-if="c.quarantine_id">#{{ c.quarantine_id }}</code><span v-else>—</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">搁置</h2>
      <p v-if="!q.length" class="sub">暂无搁置。</p>
      <table v-else>
        <thead><tr><th>id</th><th>原因</th><th>状态</th><th>事件</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="item in q" :key="item.id">
            <td>{{ item.id }}</td>
            <td><code>{{ item.reason }}</code></td>
            <td>{{ item.status }}</td>
            <td><code>{{ item.event_id || '—' }}</code></td>
            <td>{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

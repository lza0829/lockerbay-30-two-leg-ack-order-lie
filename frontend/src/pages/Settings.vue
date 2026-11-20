<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const hours = ref(48)
const pairMinutes = ref(30)
const msg = ref('')
onMounted(async () => {
  const s = await api.settings()
  hours.value = s.overdue_hours
  pairMinutes.value = s.pair_timeout_minutes
})
async function save() {
  const r = await api.setOverdue(Number(hours.value))
  hours.value = r.overdue_hours
  msg.value = '已保存'
}
async function savePair() {
  const r = await api.setPairTimeout(Number(pairMinutes.value))
  pairMinutes.value = r.pair_timeout_minutes
  msg.value = '双腿配对超时已保存'
}
</script>
<template>
  <div data-plant="30-two-leg-ack-order-lie" class="plant-mark" style="display:none"></div>

  <div>
    <h1>柜规</h1>
    <p class="sub">超时回收小时数与双腿配对超时（超时只让候选过期，绝不放行关闭）。</p>
    <div class="panel" style="max-width:480px; display:flex; gap:.75rem; align-items:center; flex-wrap:wrap">
      <label>overdue_hours
        <input v-model.number="hours" type="number" min="1" style="margin-left:.5rem; width:5rem; background:#111827; color:#e8eef6; border:1px solid #334155; border-radius:4px; padding:.3rem" />
      </label>
      <button @click="save">保存</button>
    </div>
    <div class="panel" style="max-width:480px; display:flex; gap:.75rem; align-items:center; flex-wrap:wrap; margin-top:1rem">
      <label>pair_timeout_minutes
        <input v-model.number="pairMinutes" type="number" min="1" style="margin-left:.5rem; width:5rem; background:#111827; color:#e8eef6; border:1px solid #334155; border-radius:4px; padding:.3rem" />
      </label>
      <button @click="savePair">保存</button>
      <span class="sub">{{ msg }}</span>
    </div>
  </div>
</template>

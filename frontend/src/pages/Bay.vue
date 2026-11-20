<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const overview = ref(null)
const bay = ref([])
onMounted(async () => {
  overview.value = await api.overview()
  bay.value = await api.bay()
})
</script>
<template>
  <div data-plant="30-two-leg-ack-order-lie" class="plant-mark" style="display:none"></div>

  <div>
    <h1>格阵</h1>
    <p class="sub">柜机格口占用态一眼可核；空闲 / 占用由装载事件物化。</p>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.cells }}</b><span>格口</span></div>
      <div class="stat"><b>{{ overview.occupied }}</b><span>占用</span></div>
      <div class="stat"><b>{{ overview.free }}</b><span>空闲</span></div>
      <div class="stat"><b>{{ overview.open_occupancies }}</b><span>开放占用账</span></div>
    </div>
    <div class="stats" v-if="overview?.two_leg">
      <div class="stat"><b>{{ overview.two_leg.close_legs }}</b><span>双腿关闭边</span></div>
      <div class="stat"><b>{{ overview.two_leg.pending_legs }}</b><span>待配对腿</span></div>
      <div class="stat"><b>{{ overview.two_leg.held_quarantine }}</b><span>双腿搁置</span></div>
      <div class="stat" :style="{ borderColor: overview.two_leg.violations ? 'var(--over)' : 'var(--line)' }">
        <b :style="{ color: overview.two_leg.violations ? 'var(--over)' : 'var(--free)' }">{{ overview.two_leg.violations }}</b>
        <span>栅栏违规</span>
      </div>
    </div>
    <section v-for="cab in bay" :key="cab.id" class="cabinet panel">
      <h2>{{ cab.label }} <small>{{ cab.id }} · {{ cab.site }}</small></h2>
      <div class="grid">
        <div v-for="cell in cab.cells" :key="cell.id" class="cell" :class="cell.status">
          <div>
            <div class="code">{{ cell.code }}</div>
            <div class="meta">{{ cell.size_code }} · {{ cell.status === 'occupied' ? '占用' : '空闲' }}</div>
          </div>
          <div class="meta" v-if="cell.status === 'occupied' && cell.occupancy?.tracking_no">{{ cell.occupancy.tracking_no }}</div>
          <span class="dot" />
        </div>
      </div>
    </section>
  </div>
</template>

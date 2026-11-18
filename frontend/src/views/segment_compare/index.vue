<template>
  <section class="page" data-module="segment_compare">
    <header class="page-head">
      <div>
        <h2>分群对比</h2>
        <p class="page-desc">整批提交对比任务，稳定计算分群对比结果表；每个任务独立回执，批次中断后只补算未完成任务，已完成结果不因重试而变化。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记对比任务</button>
        <button class="btn primary" data-testid="open-batch" type="button" @click="openBatch">提交整组任务</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出结果表</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div v-if="latestBatch" class="batch-bar" data-testid="batch-bar">
      <span>批次 <strong>{{ latestBatch.批次号 }}</strong></span>
      <span class="batch-status" :data-status="latestBatch.status">{{ latestBatch.status }}</span>
      <span>完成 {{ latestBatch.完成数 }}/{{ latestBatch.任务数 }}</span>
      <span>版本 v{{ latestBatch.version }}</span>
      <template v-if="pendingTasks.length">
        <span class="error-text">待补任务：{{ pendingTasks.map((p) => p.任务编号).join('、') }}</span>
        <button class="btn primary" data-testid="resume-keep" type="button" :disabled="resuming" @click="resumeBatch('keep_completed')">
          保留成功并补算
        </button>
        <button class="btn" data-testid="resume-rollback" type="button" :disabled="resuming" @click="resumeBatch('rollback_all')">
          整批回滚重算
        </button>
      </template>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.结果状态)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无对比任务，可先登记对比任务再整批提交</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showBatch" class="modal-backdrop" data-testid="batch-modal">
      <form class="modal" @submit.prevent="submitBatch">
        <h3>提交整组任务</h3>
        <p class="page-desc">一次提交全部未废弃任务，逐条给出结果与独立回执；重复提交不会改变已完成结果。</p>
        <label class="create-field">
          <span>模拟超时任务ID（逗号分隔，演示部分成功后补算）</span>
          <input v-model="simulateFailures" data-testid="simulate-failures" placeholder="例如：2" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showBatch = false">取消</button>
          <button class="btn primary" data-testid="submit-batch" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '确认提交整组' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记对比任务</h3>
        <label v-for="field in taskFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal">
        <h3>任务详情 · {{ detail.任务编号 }}</h3>
        <dl class="detail-grid">
          <template v-for="field in taskFields" :key="field">
            <dt>{{ field }}</dt><dd>{{ detail[field] ?? '—' }}</dd>
          </template>
        </dl>
        <h4>确认结果</h4>
        <dl v-if="detail.确认结果" class="detail-grid" data-testid="detail-result">
          <template v-for="field in resultFields" :key="field">
            <dt>{{ field }}</dt><dd>{{ detail.确认结果[field] ?? '—' }}</dd>
          </template>
        </dl>
        <p v-else class="empty-state">结果未确认，等待整批提交或补算</p>
        <h4>完成回执</h4>
        <dl v-if="detail.回执" class="detail-grid" data-testid="detail-receipt">
          <dt>回执编号</dt><dd>{{ detail.回执.回执编号 }}</dd>
          <dt>状态</dt><dd>{{ detail.回执.status }}</dd>
          <dt>说明</dt><dd>{{ detail.回执.message }}</dd>
        </dl>
        <p v-else class="empty-state">暂无回执</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条对比任务</span>
      <span v-if="message" class="success-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/segment_compare'
const taskFields = ["任务编号", "分群A", "分群B", "对比指标", "时间范围", "差异阈值", "显著性水平", "比较方法"]
const resultFields = ["批次号", "指标编号", "指标值", "对比基准", "差异", "p值", "显著性", "样本量A", "样本量B"]
const columns = ["任务编号", "分群A", "分群B", "对比指标", "指标值", "对比基准", "差异", "显著性", "结果状态"]
const filterFields = taskFields.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const filters = ref<Record<string, string>>({})
const latestBatch = ref<Row | null>(null)
const pendingTasks = ref<Row[]>([])
const detail = ref<Row | null>(null)
const showCreate = ref(false)
const showBatch = ref(false)
const simulateFailures = ref('')
const creating = ref(false)
const submitting = ref(false)
const resuming = ref(false)
const createValues = ref<Record<string, string>>({})
const message = ref('')
const errorMessage = ref('')

const stats = computed(() => [
  { label: '对比任务总数', value: total.value },
  { label: '已完成', value: rows.value.filter((row) => row.结果状态 === '已完成').length },
  { label: '待补算', value: pendingTasks.value.length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function openCreate() {
  createValues.value = Object.fromEntries(taskFields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

function openBatch() {
  simulateFailures.value = ''
  errorMessage.value = ''
  showBatch.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function submitBatch() {
  submitting.value = true
  errorMessage.value = ''
  message.value = ''
  const failures = simulateFailures.value
    .split(/[,，\s]+/).map((text) => Number(text.trim())).filter((num) => Number.isInteger(num) && num > 0)
  try {
    const response = await request(`${ENDPOINT}/batch`, {
      method: 'POST',
      body: JSON.stringify({ simulate_failures: failures.length ? failures : null }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '整批提交失败')
    message.value = payload.message || ''
    showBatch.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '整批提交失败'
  } finally {
    submitting.value = false
  }
}

async function resumeBatch(strategy: 'keep_completed' | 'rollback_all') {
  if (!latestBatch.value) return
  resuming.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/batch/${latestBatch.value.批次号}/resume`, {
      method: 'POST',
      body: JSON.stringify({ strategy, expected_version: latestBatch.value.version }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '续算失败')
    message.value = payload.message || ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续算失败'
    await reload()
  } finally {
    resuming.value = false
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('任务详情读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '任务详情读取失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'segment_compare.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listRes, batchRes] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/batches`),
    ])
    if (!listRes.ok) throw new Error('对比任务列表读取失败')
    const payload = await listRes.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (batchRes.ok) {
      const batchPayload = await batchRes.json()
      const batches = batchPayload.items ?? []
      latestBatch.value = batches[0] ?? null
      pendingTasks.value = latestBatch.value?.pending_tasks ?? []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对比任务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.batch-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  font-size: 13px;
}
.batch-status[data-status='已完成'] { color: #22c55e; }
.batch-status[data-status='部分完成'] { color: #b42318; }
.detail-grid {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 6px 12px;
  font-size: 13px;
  margin: 8px 0 12px;
}
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.success-text { color: #22c55e; }
h4 { margin: 12px 0 4px; font-size: 13px; }
</style>

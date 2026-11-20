<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>数据审计管理</h2>
        <p class="page-desc">
          维护审计记录；从告警事件清单读取变更，登记确认记录或更正记录，重放同一批观测核对事件数与覆盖时段。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记审计记录</button>
        <button class="btn" type="button" @click="exportRows">导出数据审计清单</button>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 审计记录台账 -->
    <template v-if="activeTab === 'records'">
      <div class="stat-row">
        <article v-for="item in stats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <form class="filter-bar" @submit.prevent="reload">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :placeholder="`按${field}检索`" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in columns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无数据审计数据，可先登记审计记录</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条数据审计记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 告警变更审计 -->
    <template v-else-if="activeTab === 'alert'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">待审计告警变更</span>
          <strong class="stat-value">{{ alertOverview['待审计变更'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前待确认处置</span>
          <strong class="stat-value">{{ alertOverview['待确认项数量'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">需单独确认的超限时段</span>
          <strong class="stat-value">{{ alertOverview['需单独确认'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">告警事件数</span>
          <strong class="stat-value">{{ alertOverview['事件数'] }}</strong>
        </article>
      </div>

      <div class="filter-bar">
        <label class="filter-item">
          <span>只看待审计</span>
          <input v-model="changesPendingOnly" type="checkbox" @change="reloadChanges" />
        </label>
        <button class="btn ghost" type="button" @click="reloadChanges">刷新变更</button>
        <span class="hint">确认类变更登记「确认记录」，追加/更正/撤销类变更登记「更正记录」。</span>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>变更编号</th>
            <th>判定编号</th>
            <th>规则 / 指标</th>
            <th>事件</th>
            <th>变更类型</th>
            <th>变更摘要</th>
            <th>建议记录类型</th>
            <th>审计状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="change in changes" :key="change['变更编号']">
            <td>{{ change['变更编号'] }}</td>
            <td>{{ change['判定编号'] || '—' }}</td>
            <td>{{ change['规则编号'] }} / {{ change['指标编号'] }}</td>
            <td>{{ change['事件'] }}</td>
            <td>{{ change['变更类型'] }}</td>
            <td class="wide-cell">{{ change['变更摘要'] }}</td>
            <td>{{ change['建议记录类型'] }}</td>
            <td>
              <span v-if="change['待审计']" class="tag warn">待审计</span>
              <span v-else class="tag ok">已登记 {{ change['审计记录'] }}</span>
            </td>
            <td class="row-actions">
              <button
                v-if="change['待审计']"
                class="link"
                type="button"
                @click="registerChange(change)"
              >
                登记{{ change['建议记录类型'] }}
              </button>
            </td>
          </tr>
          <tr v-if="!changes.length">
            <td colspan="9" class="empty-state">暂无待审计的告警变更</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- 重放核对 -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="runReplay">
        <label class="filter-item">
          <span>判定编号</span>
          <input v-model="replayForm.evaluationId" placeholder="如 EVAL-0001" required />
        </label>
        <label class="filter-item">
          <span>核对结论</span>
          <input v-model="replayForm.conclusion" placeholder="可选：值班员核对说明" />
        </label>
        <button class="btn primary" type="submit" :disabled="replaying">
          {{ replaying ? '重放中…' : '重放并登记审计' }}
        </button>
      </form>

      <p v-if="replayError" class="error-text">{{ replayError }}</p>

      <table class="data-table">
        <thead>
          <tr>
            <th>判定编号</th>
            <th>规则 / 指标</th>
            <th>判定时间</th>
            <th>本次观测</th>
            <th>事件数</th>
            <th>覆盖时段</th>
            <th>待确认项</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in evaluations" :key="item['判定编号']">
            <td>{{ item['判定编号'] }}</td>
            <td>{{ item['规则编号'] }} / {{ item['指标编号'] }}</td>
            <td>{{ item['判定时间'] }}</td>
            <td>{{ item['本次观测'].length }} 条</td>
            <td>{{ item['事件数'] }}</td>
            <td>{{ item['覆盖时段']?.start }} ~ {{ item['覆盖时段']?.end }}</td>
            <td>{{ item['待确认项数量'] }}</td>
            <td>
              <button class="link" type="button" @click="pickEvaluation(item['判定编号'])">选为重放对象</button>
            </td>
          </tr>
          <tr v-if="!evaluations.length">
            <td colspan="8" class="empty-state">暂无判定记录，先在智能告警页提交一批观测</td>
          </tr>
        </tbody>
      </table>

      <section v-if="replayResult" class="result-card">
        <header class="result-head">
          <strong>重放 {{ replayResult['判定编号'] }}</strong>
          <span class="tag" :class="replayResult['一致性'] === '一致' ? 'ok' : 'warn'">
            {{ replayResult['一致性'] }}
          </span>
          <span>{{ replayResult['差异说明'] }}</span>
        </header>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>核对项</th>
              <th>判定时</th>
              <th>重放结果</th>
              <th>是否一致</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>事件数</td>
              <td>{{ replayResult['判定时事件数'] }}</td>
              <td>{{ replayResult['处置对象'].length }}</td>
              <td>{{ replayResult['事件数一致'] ? '一致' : '不一致' }}</td>
            </tr>
            <tr>
              <td>覆盖时段</td>
              <td>
                {{ replayResult['判定时覆盖时段']?.start }} ~
                {{ replayResult['判定时覆盖时段']?.end }}
              </td>
              <td>{{ replayResult['重放时刻区间']?.start }} ~ {{ replayResult['重放时刻区间']?.end }}</td>
              <td>{{ replayResult['覆盖时段一致'] ? '一致' : '不一致' }}</td>
            </tr>
            <tr>
              <td>超限时段明细</td>
              <td>{{ replayResult['判定时超限时段'].length }} 段</td>
              <td>{{ replayResult['重放超限时段'].length }} 段</td>
              <td>{{ replayResult['时段明细一致'] ? '一致' : '不一致' }}</td>
            </tr>
            <tr>
              <td>当前待确认项数量</td>
              <td colspan="2">{{ replayResult['当前待确认项数量'] }}</td>
              <td>—</td>
            </tr>
          </tbody>
        </table>
        <table class="data-table compact">
          <thead>
            <tr><th>处置对象</th><th>状态</th><th>覆盖时段</th><th>累计超限分钟</th><th>关联变更</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in replayResult['处置对象']" :key="item['事件']">
              <td>事件 {{ item['事件'] }}</td>
              <td>{{ item['状态'] }}</td>
              <td>{{ item['覆盖时段'] }}</td>
              <td>{{ item['累计超限分钟'] }}</td>
              <td>{{ item['变更记录'].join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记审计记录</h3>
            <button class="btn ghost" type="button" @click="showCreateModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitCreate">
            <label v-for="field in columns.slice(0, 3)" :key="field" class="modal-field">
              <span>{{ field }}</span>
              <input v-model="createFields[field]" :placeholder="`请输入${field}`" required />
            </label>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showCreateModal = false">取消</button>
              <button class="btn primary" type="submit">提交登记</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type AlertChange = {
  '变更编号': string
  '判定编号': string | null
  '规则编号': string
  '指标编号': string
  '事件': number
  '变更类型': string
  '变更摘要': string
  '建议记录类型': string
  '待审计': boolean
  '审计记录': number | null
}

type EvaluationSummary = {
  '判定编号': string
  '规则编号': string
  '指标编号': string
  '判定时间': string
  '本次观测': unknown[]
  '事件数': number
  '覆盖时段': { start: string; end: string; segments: number }
  '待确认项数量': number
}

type ReplayResult = {
  '判定编号': string
  '一致性': string
  '差异说明': string
  '判定时事件数': number
  '判定时覆盖时段': { start: string; end: string }
  '重放时刻区间': { start: string; end: string }
  '判定时超限时段': { start: string; end: string }[]
  '重放超限时段': { start: string; end: string }[]
  '事件数一致': boolean
  '覆盖时段一致': boolean
  '时段明细一致': boolean
  '当前待确认项数量': number
  '处置对象': {
    '事件': number
    '状态': string
    '覆盖时段': string
    '累计超限分钟': number
    '变更记录': string[]
  }[]
}

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "操作人", "操作对象", "操作时间", "变更内容", "审计结果", "记录状态"]
const actions = ["提交审计", "标记异常"]
const statuses = ["已记录", "已审计", "异常"]
const tabs = [
  { key: 'records', label: '审计记录' },
  { key: 'alert', label: '告警变更审计' },
  { key: 'replay', label: '重放核对' },
]
const stats = [
  { label: '审计记录数', value: 0 },
  { label: '异常记录数', value: 0 },
  { label: '已审计记录数', value: 0 },
]

const activeTab = ref('records')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreateModal = ref(false)
const createFields = ref<Record<string, string>>({})

const changes = ref<AlertChange[]>([])
const changesPendingOnly = ref(true)
const alertOverview = ref<Record<string, number>>({})
const evaluations = ref<EvaluationSummary[]>([])
const replayForm = ref({ evaluationId: '', conclusion: '' })
const replayResult = ref<ReplayResult | null>(null)
const replayError = ref('')
const replaying = ref(false)

function updateStats() {
  const data = rows.value
  stats[0].value = data.length
  stats[1].value = data.filter((row: Row) => row.status === statuses[2]).length
  stats[2].value = data.filter((row: Row) => row.status === statuses[1]).length
}

function switchTab(key: string) {
  activeTab.value = key
  errorMessage.value = ''
  if (key === 'alert') {
    void reloadChanges()
  } else if (key === 'replay') {
    void reloadEvaluations()
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = ''
  createFields.value = {}
  showCreateModal.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createFields.value }),
    })
    if (!response.ok) {
      const data = await response.json()
      throw new Error(data.message || '登记失败')
    }
    showCreateModal.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('数据审计动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计操作失败'
  }
}

async function reloadChanges() {
  errorMessage.value = ''
  try {
    const [changeResponse, overviewResponse] = await Promise.all([
      request(`${ENDPOINT}/alert/changes?pending_only=${changesPendingOnly.value}`),
      request(`${ENDPOINT}/alert/overview`),
    ])
    if (!changeResponse.ok || !overviewResponse.ok) {
      throw new Error('告警变更读取失败')
    }
    const changePayload = await changeResponse.json()
    const overviewPayload = await overviewResponse.json()
    changes.value = changePayload.items ?? []
    alertOverview.value = overviewPayload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警变更读取失败'
  }
}

async function registerChange(change: AlertChange) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/alert/changes/${change['变更编号']}/audit`, {
      method: 'POST',
      body: JSON.stringify({ values: { 记录类型: change['建议记录类型'] } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '审计登记未生效')
    }
    await Promise.all([reloadChanges(), reload()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审计登记失败'
  }
}

async function reloadEvaluations() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/alert/evaluations`)
    if (!response.ok) {
      throw new Error('判定记录读取失败')
    }
    const payload = await response.json()
    evaluations.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '判定记录读取失败'
  }
}

function pickEvaluation(evaluationId: string) {
  replayForm.value.evaluationId = evaluationId
}

async function runReplay() {
  replayError.value = ''
  if (!replayForm.value.evaluationId.trim()) {
    replayError.value = '请填写判定编号'
    return
  }
  replaying.value = true
  try {
    const response = await request(
      `${ENDPOINT}/alert/evaluations/${replayForm.value.evaluationId.trim()}/replay`,
      {
        method: 'POST',
        body: JSON.stringify({ remark: replayForm.value.conclusion || undefined }),
      },
    )
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '重放未完成')
    }
    replayResult.value = payload.entry['重放'] as ReplayResult
    await Promise.all([reload(), reloadChanges()])
  } catch (error) {
    replayError.value = error instanceof Error ? error.message : '重放未完成'
  } finally {
    replaying.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计列表读取失败'
  }
}

onMounted(reload)
</script>

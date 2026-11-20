<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>智能告警管理</h2>
        <p class="page-desc">
          维护告警规则；按规则、指标编号和观测的业务时刻做阈值判定，生成可核对的告警事件清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记告警规则</button>
        <button class="btn" type="button" @click="exportRows">导出智能告警清单</button>
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

    <!-- 规则台账 -->
    <template v-if="activeTab === 'rules'">
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
            <td :colspan="columns.length + 1" class="empty-state">暂无智能告警数据，可先登记告警规则</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条智能告警记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <!-- 阈值判定 -->
    <template v-else-if="activeTab === 'evaluate'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">待确认处置</span>
          <strong class="stat-value">{{ eventSummary.pending_total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">需单独确认</span>
          <strong class="stat-value">{{ eventSummary.review_required_total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">事件总数</span>
          <strong class="stat-value">{{ eventSummary.total }}</strong>
        </article>
      </div>

      <form class="eval-form" @submit.prevent="submitEvaluate">
        <div class="eval-head">
          <label class="filter-item">
            <span>规则编号</span>
            <input v-model="evalForm.ruleCode" placeholder="如 ALER-0001" required />
          </label>
          <label class="filter-item">
            <span>指标编号</span>
            <input v-model="evalForm.metricCode" placeholder="如 METR-0001" required />
          </label>
          <label class="filter-item">
            <span>合并口径</span>
            <select v-model="evalForm.mergeHint">
              <option value="">按规则阈值条件</option>
              <option value="merge">保留一项处置（merge）</option>
              <option value="split">拆出待确认处置（split）</option>
            </select>
          </label>
        </div>

        <table class="data-table compact">
          <thead>
            <tr>
              <th>业务时刻</th>
              <th>观测值</th>
              <th>缺测</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in evalForm.observations" :key="index">
              <td><input v-model="item.moment" placeholder="2026-09-10 10:00" /></td>
              <td><input v-model="item.value" placeholder="数值" :disabled="item.missing" /></td>
              <td>
                <input v-model="item.missing" type="checkbox" />
              </td>
              <td>
                <button class="link" type="button" @click="removeObservation(index)">移除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="eval-actions">
          <button class="btn ghost" type="button" @click="addObservation">增加一条观测</button>
          <button class="btn primary" type="submit" :disabled="submitting">
            {{ submitting ? '判定中…' : '提交判定' }}
          </button>
          <span class="hint">
            迟到观测按业务时刻插回重算；同一批观测重复提交不会扩大事件计数。
          </span>
        </div>
        <p v-if="evalError" class="error-text">{{ evalError }}</p>
      </form>

      <section v-if="evalResult" class="result-card">
        <header class="result-head">
          <strong>判定 {{ evalResult.判定编号 }}</strong>
          <span class="tag" :class="evalResult.合并口径 === 'split' ? 'warn' : 'ok'">
            {{ evalResult.合并口径 === 'split' ? '拆分口径' : '合并口径' }}
          </span>
          <span>事件数 {{ evalResult.事件数 }}</span>
          <span>待确认项 {{ evalResult.待确认项数量 }}</span>
          <span v-if="evalResult.待确认变化">（{{ evalResult.待确认变化 > 0 ? '+' : '' }}{{ evalResult.待确认变化 }}）</span>
          <span v-if="evalResult.需单独确认的事件.length" class="tag warn">
            需单独确认 {{ evalResult.需单独确认的事件.join('、') }}
          </span>
        </header>
        <p class="result-desc">{{ evalResult.说明 }}</p>

        <table class="data-table compact">
          <thead>
            <tr><th>超限时段</th><th>时长（分钟）</th><th>收口方式</th></tr>
          </thead>
          <tbody>
            <tr v-for="segment in evalResult.超限时段" :key="`${segment.start}-${segment.end}`">
              <td>{{ segment.start }} ~ {{ segment.end }}</td>
              <td>{{ segment.duration_minutes }}</td>
              <td>{{ segment.ending_text }}</td>
            </tr>
            <tr v-if="!evalResult.超限时段.length">
              <td colspan="3" class="empty-state">本批观测未形成超限时段</td>
            </tr>
          </tbody>
        </table>

        <div v-if="evalResult.缺测依据.length" class="gap-list">
          <span class="tag">缺测依据</span>
          <span v-for="(gap, index) in evalResult.缺测依据" :key="index">
            {{ gap.start }} ~ {{ gap.end }}（{{ gap.reason }}）
          </span>
        </div>
      </section>
    </template>

    <!-- 事件清单 -->
    <template v-else>
      <form class="filter-bar" @submit.prevent="reloadEvents">
        <label class="filter-item">
          <span>规则编号</span>
          <input v-model="eventFilters.ruleCode" placeholder="按规则编号过滤" />
        </label>
        <label class="filter-item">
          <span>指标编号</span>
          <input v-model="eventFilters.metricCode" placeholder="按指标编号过滤" />
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="eventFilters.status">
            <option value="">全部</option>
            <option v-for="status in eventStatuses" :key="status" :value="status">{{ status }}</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetEventFilters">重置条件</button>
      </form>

      <p class="hint">
        待确认 {{ eventSummary.pending_total }} 项 · 已确认但需单独确认
        {{ eventSummary.review_required_total }} 项
      </p>

      <table class="data-table">
        <thead>
          <tr>
            <th>事件</th>
            <th>规则 / 指标</th>
            <th>状态</th>
            <th>覆盖时段</th>
            <th>超限时段</th>
            <th>累计超限分钟</th>
            <th>确认人 / 时间</th>
            <th>需单独确认</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="String(event.id)">
            <td>{{ event['事件序号'] }}</td>
            <td>{{ event['规则编号'] }} / {{ event['指标编号'] }}</td>
            <td>{{ event['状态'] }}</td>
            <td>{{ event['起始时刻'] }} ~ {{ event['结束时刻'] }}</td>
            <td>
              <div v-for="segment in event['超限时段']" :key="`${segment.start}-${segment.end}`">
                {{ segment.start }} ~ {{ segment.end }}（{{ segment.duration_minutes }} 分钟）
              </div>
            </td>
            <td>{{ event['累计超限分钟'] }}</td>
            <td>{{ event['确认人'] || '—' }}<br /><small>{{ event['确认时间'] || '' }}</small></td>
            <td>
              <span v-if="event.review_required" class="tag warn">{{ event.review_note }}</span>
              <span v-else>—</span>
            </td>
            <td class="row-actions">
              <button
                v-for="action in eventActions(event)"
                :key="action.label"
                class="link"
                type="button"
                @click="runEventAction(action.label, event)"
              >
                {{ action.label }}
              </button>
            </td>
          </tr>
          <tr v-if="!events.length">
            <td colspan="9" class="empty-state">暂无告警事件，先在上方提交一批观测做阈值判定</td>
          </tr>
        </tbody>
      </table>

      <details v-if="selectedEvent" class="evidence-card" open>
        <summary>判定依据：事件 {{ selectedEvent['事件序号'] }}</summary>
        <p class="result-desc">{{ selectedEvent['规则快照'] && selectedEvent['规则快照']['阈值条件'] }}</p>
        <table class="data-table compact">
          <thead>
            <tr><th>业务时刻</th><th>观测值</th><th>判定</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in selectedEvent['依据观测']" :key="item['业务时刻']">
              <td>{{ item['业务时刻'] }}</td>
              <td>{{ item['缺测'] ? '缺测' : item['观测值'] }}</td>
              <td>{{ item['判定'] }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="selectedEvent['排除的恢复观测'].length" class="hint">
          已排除的恢复观测：
          <span v-for="item in selectedEvent['排除的恢复观测']" :key="item['业务时刻']">
            {{ item['业务时刻'] }}={{ item['观测值'] }}；
          </span>
        </p>
        <p v-if="selectedEvent['缺测依据'].length" class="hint">
          缺测依据：
          <span v-for="(gap, index) in selectedEvent['缺测依据']" :key="index">
            {{ gap.start }} ~ {{ gap.end }}（{{ gap.reason }}）；
          </span>
        </p>
        <table v-if="selectedEvent['变更记录'].length" class="data-table compact">
          <thead>
            <tr><th>变更编号</th><th>类型</th><th>摘要</th><th>操作人</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="change in selectedEvent['变更记录']" :key="change['变更编号']">
              <td>{{ change['变更编号'] }}</td>
              <td>{{ change['变更类型'] }}</td>
              <td>{{ change['变更摘要'] }}</td>
              <td>{{ change['操作人'] }}</td>
              <td>{{ change['操作时间'] }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <footer class="page-foot">
        <span>共 {{ events.length }} 条告警事件</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记告警规则</h3>
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

type ObservationInput = {
  moment: string
  value: string
  missing: boolean
}

type Observation = {
  '业务时刻': string
  '观测值': number | null
  '缺测': boolean
  '判定': string
}

type Interval = {
  start: string
  end: string
  duration_minutes: number
  ending: string
  ending_text: string
}

type EventChange = {
  '变更编号': string
  '变更类型': string
  '变更摘要': string
  '操作人': string
  '操作时间': string
}

type AlertEvent = {
  id: number
  '事件序号': string
  '规则编号': string
  '指标编号': string
  '状态': string
  '起始时刻': string
  '结束时刻': string
  '累计超限分钟': number
  '超限时段': Interval[]
  '依据观测': Observation[]
  '排除的恢复观测': { '业务时刻': string; '观测值': number | null }[]
  '缺测依据': { start: string; end: string; reason: string }[]
  '规则快照'?: { '阈值条件'?: string }
  '确认人': string | null
  '确认时间': string | null
  '确认时的时段': Interval[]
  '待确认时段': string[]
  '变更记录': EventChange[]
  review_required: boolean
  review_note: string | null
}

type EvaluationResult = {
  '判定编号': string
  '合并口径': string
  '事件数': number
  '待确认项数量': number
  '待确认变化': number
  '需单独确认的事件': number[]
  '超限时段': Interval[]
  '缺测依据': { start: string; end: string; reason: string }[]
  '关联事件': AlertEvent[]
  '说明': string
}

const ENDPOINT = '/api/alert'
const columns = ["规则编号", "告警名称", "监控指标", "阈值条件", "告警级别", "通知方式", "静默时段", "规则状态"]
const actions = ["保存配置", "启用规则", "停用规则"]
const statuses = ["草稿", "已配置", "生效中", "已停用"]
const eventStatuses = ["待确认", "已确认", "已撤销"]
const tabs = [
  { key: 'rules', label: '告警规则' },
  { key: 'evaluate', label: '阈值判定' },
  { key: 'events', label: '事件清单' },
]
const stats = [{"label": "生效中规则", "value": 0}, {"label": "草稿规则", "value": 0}, {"label": "停用规则", "value": 0}]

const activeTab = ref('rules')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreateModal = ref(false)
const createFields = ref<Record<string, string>>({})

const evalForm = ref({
  ruleCode: '',
  metricCode: '',
  mergeHint: '',
  observations: [
    { moment: '', value: '', missing: false },
  ] as ObservationInput[],
})
const evalResult = ref<EvaluationResult | null>(null)
const evalError = ref('')
const submitting = ref(false)

const events = ref<AlertEvent[]>([])
const eventFilters = ref({ ruleCode: '', metricCode: '', status: '' })
const eventSummary = ref({ total: 0, pending_total: 0, review_required_total: 0 })
const selectedEvent = ref<AlertEvent | null>(null)

function updateStats() {
  const data = rows.value
  stats.length > 0 && (stats[0].value = data.filter((r: Row) => r.status === statuses[2]).length)
  stats.length > 1 && (stats[1].value = data.filter((r: Row) => r.status === statuses[0]).length)
  stats.length > 2 && (stats[2].value = data.filter((r: Row) => r.status === statuses[3]).length)
}

function switchTab(key: string) {
  activeTab.value = key
  errorMessage.value = ''
  if (key === 'evaluate' || key === 'events') {
    void reloadEvents()
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function resetEventFilters() {
  eventFilters.value = { ruleCode: '', metricCode: '', status: '' }
  void reloadEvents()
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
      throw new Error('智能告警动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '智能告警操作失败'
  }
}

function addObservation() {
  evalForm.value.observations.push({ moment: '', value: '', missing: false })
}

function removeObservation(index: number) {
  evalForm.value.observations.splice(index, 1)
  if (!evalForm.value.observations.length) {
    addObservation()
  }
}

async function submitEvaluate() {
  evalError.value = ''
  const observations = evalForm.value.observations
    .filter((item) => item.moment.trim())
    .map((item) => {
      const record: Record<string, unknown> = { 业务时刻: item.moment.trim() }
      if (item.missing || item.value.trim() === '') {
        record['缺测'] = true
      } else {
        record['观测值'] = Number(item.value)
      }
      return record
    })
  if (!observations.length) {
    evalError.value = '请至少填写一条带业务时刻的观测'
    return
  }
  submitting.value = true
  try {
    const values: Record<string, unknown> = {
      规则编号: evalForm.value.ruleCode.trim(),
      指标编号: evalForm.value.metricCode.trim(),
      观测: observations,
    }
    if (evalForm.value.mergeHint === 'split') {
      values['合并口径'] = 'split'
    }
    const response = await request(`${ENDPOINT}/evaluate`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '判定未执行')
    }
    evalResult.value = payload.entry as EvaluationResult
    await reloadEvents()
  } catch (error) {
    evalError.value = error instanceof Error ? error.message : '判定未执行'
  } finally {
    submitting.value = false
  }
}

function eventActions(event: AlertEvent): { label: string; key: string }[] {
  const items: { label: string; key: string }[] = []
  if (event['状态'] === '待确认') {
    items.push({ label: '确认处置', key: '确认处置' })
    items.push({ label: '撤销待确认', key: '撤销待确认' })
  }
  if (event.review_required) {
    items.push({ label: '确认新增时段', key: '确认新增时段' })
  }
  if (event['状态'] === '已确认') {
    items.push({ label: '更正处置', key: '更正处置' })
  }
  return items
}

async function runEventAction(label: string, event: AlertEvent) {
  errorMessage.value = ''
  const values: Record<string, unknown> = {
    action: label === '确认新增时段' ? '确认处置' : label,
    规则编号: event['规则编号'],
    指标编号: event['指标编号'],
  }
  if (label === '确认新增时段') {
    values['时段'] = event['待确认时段']?.[0] ?? event['超限时段'][0]?.start
  }
  try {
    const response = await request(`${ENDPOINT}/events/${event.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '告警事件动作未生效')
    }
    await reloadEvents()
    selectedEvent.value = events.value.find((item) => item.id === event.id) ?? null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警事件操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('告警规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '智能告警列表读取失败'
  }
}

async function reloadEvents() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (eventFilters.value.ruleCode.trim()) params.set('rule_code', eventFilters.value.ruleCode.trim())
  if (eventFilters.value.metricCode.trim()) params.set('metric_code', eventFilters.value.metricCode.trim())
  if (eventFilters.value.status) params.set('status', eventFilters.value.status)
  try {
    const response = await request(`${ENDPOINT}/events?${params.toString()}`)
    if (!response.ok) {
      throw new Error('告警事件清单读取失败')
    }
    const payload = await response.json()
    events.value = payload.items ?? []
    eventSummary.value = {
      total: payload.total ?? 0,
      pending_total: payload.pending_total ?? 0,
      review_required_total: payload.review_required_total ?? 0,
    }
    if (selectedEvent.value) {
      selectedEvent.value =
        events.value.find((item) => item.id === selectedEvent.value?.id) ?? events.value[0] ?? null
    } else if (events.value.length) {
      selectedEvent.value = events.value[0]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警事件清单读取失败'
  }
}

onMounted(reload)
</script>

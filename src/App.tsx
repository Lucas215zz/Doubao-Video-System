import {
  AlertCircle,
  CheckCircle2,
  CheckSquare2,
  Clock3,
  Download,
  ImagePlus,
  KeyRound,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Settings,
  Server,
  Square,
  Trash2,
  Upload,
  X,
  XCircle,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent, FormEvent } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_DOUBAO_API_BASE || 'http://127.0.0.1:8034'

type CookieItem = {
  id: number
  name: string
  filename: string
  path: string
  size: number
  credits: number | null
  remain_count: number | null
  has_generating_task: boolean
  is_beta_user: boolean
  last_used: string | null
  last_error: string | null
  status: string
  enabled: boolean
}

type TaskStatus = 'pending' | 'running' | 'submitted' | 'success' | 'failed'
type TaskStatusFilter = 'all' | TaskStatus
type TaskAttachment = {
  index: number
  type: string
  fileName: string
  size: number
  mime?: string
  width?: number
  height?: number
  url?: string
}
type Task = {
  task_id: string
  prompt: string
  ratio: string
  model: string
  duration: number
  status: TaskStatus
  progress: number
  cookie_name?: string | null
  cookie_file?: string | null
  video_path?: string | null
  video_url?: string | null
  download_url?: string | null
  error_message?: string | null
  attachments_count?: number
  attachments?: TaskAttachment[]
  created_at?: string | null
  started_at?: string | null
  completed_at?: string | null
}

type Health = {
  status: string
  service: string
  version: string
  cookies_count: number
  running_tasks: number
  max_workers: number
  runtime_ready?: boolean
  runtime_missing?: string[]
}

type RuntimeConfig = {
  config: {
    common_params: Record<string, string>
    fp: string
    updated_at?: string
    config_path?: string
  }
  diagnostics: {
    ready: boolean
    missing: string[]
    params_count: number
    has_fp: boolean
  }
}

type Toast = {
  text: string
  tone: 'success' | 'error' | 'info'
}

type ReferenceImage = {
  id: string
  file: File
  url: string
}

const emptyHealth: Health = {
  status: 'offline',
  service: 'doubao-cookie-pool-video',
  version: '-',
  cookies_count: 0,
  running_tasks: 0,
  max_workers: 0,
}

const statusLabels: Record<TaskStatus, string> = {
  pending: '等待中',
  running: '生成中',
  submitted: '已提交',
  success: '已完成',
  failed: '失败',
}

const statusIcons: Record<TaskStatus, typeof Clock3> = {
  pending: Clock3,
  running: Loader2,
  submitted: Play,
  success: CheckCircle2,
  failed: XCircle,
}

const taskFilterLabels: Record<TaskStatusFilter, string> = {
  all: '全部',
  pending: '等待中',
  running: '生成中',
  submitted: '已提交',
  success: '生成完成',
  failed: '失败',
}

function App() {
  const [activeTab, setActiveTab] = useState<'create' | 'cookies' | 'tasks' | 'settings'>('create')
  const [taskStatusFilter, setTaskStatusFilter] = useState<TaskStatusFilter>('all')
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([])
  const [health, setHealth] = useState<Health>(emptyHealth)
  const [cookies, setCookies] = useState<CookieItem[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [toast, setToast] = useState<Toast | null>(null)
  const [cookieName, setCookieName] = useState('')
  const [cookiePaste, setCookiePaste] = useState('')
  const [referenceImages, setReferenceImages] = useState<ReferenceImage[]>([])
  const [referencesDragging, setReferencesDragging] = useState(false)
  const referenceImagesRef = useRef<ReferenceImage[]>([])
  const [taskForm, setTaskForm] = useState({
    prompt: '',
    ratio: '16:9',
    model: 'doubao-seedance-2.0',
    duration: 10,
    cookie_file: '',
    attachments: '',
  })
  const [runtimeForm, setRuntimeForm] = useState({
    fp: '',
    common_params: '',
  })

  const enabledCookies = useMemo(() => cookies.filter((cookie) => cookie.enabled), [cookies])
  const availableCredits = useMemo(() => {
    return cookies.reduce((sum, cookie) => sum + Math.max(0, Number(cookie.remain_count ?? cookie.credits ?? 0)), 0)
  }, [cookies])
  const taskStats = useMemo(() => {
    return tasks.reduce(
      (acc, task) => {
        acc[task.status] += 1
        return acc
      },
      { pending: 0, running: 0, submitted: 0, success: 0, failed: 0 } as Record<TaskStatus, number>,
    )
  }, [tasks])
  const filteredTasks = useMemo(() => {
    if (taskStatusFilter === 'all') return tasks
    if (taskStatusFilter === 'running') {
      return tasks.filter((task) => task.status === 'pending' || task.status === 'running')
    }
    return tasks.filter((task) => task.status === taskStatusFilter)
  }, [tasks, taskStatusFilter])
  const filteredTaskIds = useMemo(() => filteredTasks.map((task) => task.task_id), [filteredTasks])
  const selectedVisibleCount = useMemo(
    () => filteredTaskIds.filter((taskId) => selectedTaskIds.includes(taskId)).length,
    [filteredTaskIds, selectedTaskIds],
  )
  const allVisibleTasksSelected = filteredTaskIds.length > 0 && selectedVisibleCount === filteredTaskIds.length

  const showToast = useCallback((text: string, tone: Toast['tone'] = 'info') => {
    setToast({ text, tone })
    window.setTimeout(() => setToast(null), tone === 'error' ? 4200 : 2400)
  }, [])

  const loadData = useCallback(
    async (showLoader = false) => {
      if (showLoader) setLoading(true)
      try {
        const [nextHealth, cookieResult, taskResult, runtimeResult] = await Promise.all([
          api<Health>('/api/health'),
          api<{ cookies: CookieItem[] }>('/api/cookies'),
          api<{ tasks: Task[] }>('/api/tasks?limit=100'),
          api<RuntimeConfig>('/api/runtime-config'),
        ])
        setHealth(nextHealth)
        setCookies(cookieResult.cookies || [])
        setTasks(taskResult.tasks || [])
        setRuntimeConfig(runtimeResult)
        setRuntimeForm((current) => {
          if (current.fp || current.common_params) return current
          return {
            fp: runtimeResult.config.fp || '',
            common_params: JSON.stringify(runtimeResult.config.common_params || {}, null, 2),
          }
        })
      } catch (error) {
        showToast(readError(error), 'error')
      } finally {
        if (showLoader) setLoading(false)
      }
    },
    [showToast],
  )

  useEffect(() => {
    void loadData(true)
    const timer = window.setInterval(() => void loadData(false), 8000)
    return () => window.clearInterval(timer)
  }, [loadData])

  useEffect(() => {
    referenceImagesRef.current = referenceImages
  }, [referenceImages])

  useEffect(() => {
    const existingTaskIds = new Set(tasks.map((task) => task.task_id))
    setSelectedTaskIds((current) => current.filter((taskId) => existingTaskIds.has(taskId)))
  }, [tasks])

  useEffect(() => {
    return () => {
      referenceImagesRef.current.forEach((item) => URL.revokeObjectURL(item.url))
    }
  }, [])

  async function withBusy(key: string, action: () => Promise<void>) {
    setBusy((current) => ({ ...current, [key]: true }))
    try {
      await action()
    } finally {
      setBusy((current) => ({ ...current, [key]: false }))
    }
  }

  async function uploadCookieFile(file: File | null) {
    if (!file) return
    await withBusy('cookie-upload', async () => {
      const form = new FormData()
      form.set('file', file)
      form.set('name', cookieName || file.name.replace(/\.json$/i, ''))
      await api('/api/cookies', { method: 'POST', body: form })
      setCookieName('')
      showToast('Cookie 已上传', 'success')
      await loadData(false)
    })
  }

  async function uploadPastedCookie(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!cookiePaste.trim()) {
      showToast('Cookie JSON 不能为空', 'error')
      return
    }
    await withBusy('cookie-paste', async () => {
      JSON.parse(cookiePaste)
      await api('/api/cookies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: cookieName || `cookie_${Date.now()}`,
          content: cookiePaste,
        }),
      })
      setCookieName('')
      setCookiePaste('')
      showToast('Cookie 已保存', 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function testCookie(cookie: CookieItem) {
    await withBusy(`test-${cookie.name}`, async () => {
      const result = await api<{ credits?: number | null; remain_count?: number | null }>(
        `/api/cookies/${encodeURIComponent(cookie.filename)}/test`,
        { method: 'POST' },
      )
      showToast(`${cookie.name}: ${result.remain_count ?? result.credits ?? '未知'} 次`, 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function checkAllCookies() {
    await withBusy('check-all', async () => {
      await api('/api/cookies/check-all', { method: 'POST' })
      showToast('Cookie 池已检测', 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function toggleCookie(cookie: CookieItem) {
    await withBusy(`toggle-${cookie.name}`, async () => {
      await api(`/api/cookies/${encodeURIComponent(cookie.filename)}/status`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !cookie.enabled }),
      })
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function deleteCookie(cookie: CookieItem) {
    if (!window.confirm(`删除 Cookie：${cookie.name}？`)) return
    await withBusy(`delete-${cookie.name}`, async () => {
      await api(`/api/cookies/${encodeURIComponent(cookie.filename)}`, { method: 'DELETE' })
      showToast('Cookie 已删除', 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  function appendReferenceFiles(files: FileList | File[]) {
    const incoming = Array.from(files)
    if (!incoming.length) return
    const accepted = incoming.filter((file) => file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name))
    if (accepted.length !== incoming.length) {
      showToast('参考图只支持 png/jpg/jpeg/webp', 'error')
    }
    if (!accepted.length) return

    const nextImages = accepted.map((file) => ({
      id: `${file.name}-${file.size}-${file.lastModified}-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`,
      file,
      url: URL.createObjectURL(file),
    }))
    setReferenceImages((current) => [...current, ...nextImages])
  }

  function removeReferenceImage(id: string) {
    setReferenceImages((current) => {
      const target = current.find((item) => item.id === id)
      if (target) URL.revokeObjectURL(target.url)
      return current.filter((item) => item.id !== id)
    })
  }

  function clearReferenceImages() {
    setReferenceImages((current) => {
      current.forEach((item) => URL.revokeObjectURL(item.url))
      return []
    })
  }

  function handleReferenceDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault()
    setReferencesDragging(false)
    appendReferenceFiles(event.dataTransfer.files)
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!taskForm.prompt.trim()) {
      showToast('提示词不能为空', 'error')
      return
    }

    await withBusy('submit-task', async () => {
      const attachments = taskForm.attachments.trim() ? JSON.parse(taskForm.attachments) : []
      if (!Array.isArray(attachments)) {
        throw new Error('附件 JSON 必须是数组')
      }

      if (referenceImages.length) {
        const form = new FormData()
        form.set('prompt', taskForm.prompt.trim())
        form.set('ratio', taskForm.ratio)
        form.set('model', taskForm.model)
        form.set('duration', String(taskForm.duration))
        if (taskForm.cookie_file) form.set('cookie_file', taskForm.cookie_file)
        if (taskForm.attachments.trim()) form.set('attachments', JSON.stringify(attachments))
        referenceImages.forEach((item) => form.append('files', item.file))
        await api('/v1/videos/generations', {
          method: 'POST',
          body: form,
        })
      } else {
        await api('/v1/videos/generations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: taskForm.prompt.trim(),
            ratio: taskForm.ratio,
            model: taskForm.model,
            duration: Number(taskForm.duration),
            cookie_file: taskForm.cookie_file || undefined,
            attachments,
          }),
        })
      }
      setTaskForm((current) => ({ ...current, prompt: '', attachments: '' }))
      clearReferenceImages()
      setActiveTab('tasks')
      showToast('任务已提交到 Cookie 池', 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  function toggleTaskSelection(taskId: string) {
    setSelectedTaskIds((current) =>
      current.includes(taskId) ? current.filter((item) => item !== taskId) : [...current, taskId],
    )
  }

  function toggleVisibleTaskSelection() {
    setSelectedTaskIds((current) => {
      if (allVisibleTasksSelected) {
        return current.filter((taskId) => !filteredTaskIds.includes(taskId))
      }
      return Array.from(new Set([...current, ...filteredTaskIds]))
    })
  }

  async function clearSelectedTasks() {
    if (!selectedTaskIds.length) {
      showToast('请先选择要删除的任务', 'error')
      return
    }
    if (!window.confirm(`删除选中的 ${selectedTaskIds.length} 个任务？`)) return
    await withBusy('clear-tasks', async () => {
      await api('/api/tasks/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: selectedTaskIds }),
      })
      showToast('已删除所选任务', 'success')
      setSelectedTaskIds([])
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function retryTask(task: Task) {
    await withBusy(`retry-${task.task_id}`, async () => {
      const result = await api<{ task_id: string }>(`/api/task/${encodeURIComponent(task.task_id)}/retry`, { method: 'POST' })
      showToast(`已重新提交：${result.task_id.slice(0, 8)}`, 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function downloadOriginalVideo(task: Task) {
    await withBusy(`original-${task.task_id}`, async () => {
      await api(`/api/task/${encodeURIComponent(task.task_id)}/download-original`, { method: 'POST' })
      showToast('视频已补抓并写回任务', 'success')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  async function saveRuntimeConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    await withBusy('runtime-config', async () => {
      const result = await api<RuntimeConfig>('/api/runtime-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fp: runtimeForm.fp,
          common_params: runtimeForm.common_params,
        }),
      })
      setRuntimeConfig(result)
      setRuntimeForm({
        fp: result.config.fp || '',
        common_params: JSON.stringify(result.config.common_params || {}, null, 2),
      })
      showToast(result.diagnostics.ready ? '运行时参数已保存' : `仍缺少：${result.diagnostics.missing.join(', ')}`, result.diagnostics.ready ? 'success' : 'error')
      await loadData(false)
    }).catch((error) => showToast(readError(error), 'error'))
  }

  if (loading) {
    return (
      <main className="loading">
        <Loader2 className="spin" size={22} />
        正在连接豆包 Cookie 池
      </main>
    )
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>豆包视频 Cookie 池</h1>
          <p>{API_BASE}</p>
        </div>
        <div className={`service-pill ${health.status === 'healthy' ? 'online' : 'offline'}`}>
          <Server size={16} />
          {health.status === 'healthy' ? `服务正常 v${health.version}` : '服务未连接'}
        </div>
      </header>

      {health.runtime_ready === false && (
        <div className="notice">
          <AlertCircle size={16} />
          自动运行时参数仍缺少：{(health.runtime_missing || []).join(', ')}。请刷新或打开“运行时参数”查看。
        </div>
      )}

      {toast && <div className={`toast ${toast.tone}`}>{toast.text}</div>}

      <section className="stats-grid" aria-label="运行状态">
        <StatCard label="Cookie" value={cookies.length} detail={`${enabledCookies.length} 个启用`} tone="blue" />
        <StatCard label="剩余次数" value={availableCredits} detail="来自额度缓存" tone="green" />
        <StatCard label="生成中" value={taskStats.running + taskStats.submitted} detail="后台轮询" tone="amber" />
        <StatCard label="失败" value={taskStats.failed} detail={`${taskStats.success} 个完成`} tone="red" />
      </section>

      <nav className="tabs" aria-label="页面">
        <button className={activeTab === 'create' ? 'active' : ''} onClick={() => setActiveTab('create')}>
          <Plus size={16} />
          创建任务
        </button>
        <button className={activeTab === 'cookies' ? 'active' : ''} onClick={() => setActiveTab('cookies')}>
          <KeyRound size={16} />
          Cookie 管理
        </button>
        <button className={activeTab === 'tasks' ? 'active' : ''} onClick={() => setActiveTab('tasks')}>
          <Play size={16} />
          任务列表
        </button>
        <button className={activeTab === 'settings' ? 'active' : ''} onClick={() => setActiveTab('settings')}>
          <Settings size={16} />
          运行时参数
        </button>
        <button className="ghost" onClick={() => void loadData(false)} title="刷新">
          <RefreshCw size={16} />
        </button>
      </nav>

      {activeTab === 'create' && (
        <section className="create-layout">
          <form className="tool-panel create-panel" onSubmit={submitTask}>
            <div className="section-title">
              <Play size={18} />
              <h2>创建视频任务</h2>
            </div>
            <label className="field full">
              <span>提示词</span>
              <textarea
                value={taskForm.prompt}
                onChange={(event) => setTaskForm({ ...taskForm, prompt: event.target.value })}
                placeholder="描述画面、动作、镜头、风格"
                required
              />
            </label>
            <div className="form-grid">
              <label className="field">
                <span>比例</span>
                <select value={taskForm.ratio} onChange={(event) => setTaskForm({ ...taskForm, ratio: event.target.value })}>
                  <option value="16:9">16:9 横屏</option>
                  <option value="9:16">9:16 竖屏</option>
                  <option value="1:1">1:1 方形</option>
                  <option value="4:3">4:3</option>
                  <option value="3:4">3:4</option>
                </select>
              </label>
              <label className="field">
                <span>模型</span>
                <select value={taskForm.model} onChange={(event) => setTaskForm({ ...taskForm, model: event.target.value })}>
                  <option value="doubao-seedance-2.0">Seedance 2.0</option>
                  <option value="doubao-video-generation">视频生成</option>
                </select>
              </label>
              <label className="field">
                <span>时长</span>
                <select
                  value={taskForm.duration}
                  onChange={(event) => setTaskForm({ ...taskForm, duration: Number(event.target.value) })}
                >
                  <option value={5}>5 秒</option>
                  <option value={10}>10 秒</option>
                  <option value={15}>15 秒</option>
                </select>
              </label>
              <label className="field">
                <span>指定 Cookie</span>
                <select
                  value={taskForm.cookie_file}
                  onChange={(event) => setTaskForm({ ...taskForm, cookie_file: event.target.value })}
                >
                  <option value="">自动轮询</option>
                  {enabledCookies.map((cookie) => (
                    <option key={cookie.filename} value={cookie.filename}>
                      {cookie.name} · {cookie.remain_count ?? cookie.credits ?? '未知'} 次
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="reference-uploader field full">
              <span>参考图片</span>
              <label
                className={`reference-dropzone ${referencesDragging ? 'dragging' : ''} ${referenceImages.length ? 'has-images' : ''}`}
                onDrop={handleReferenceDrop}
                onDragOver={(event) => {
                  event.preventDefault()
                  setReferencesDragging(true)
                }}
                onDragLeave={() => setReferencesDragging(false)}
              >
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  multiple
                  onChange={(event) => {
                    appendReferenceFiles(event.target.files || [])
                    event.currentTarget.value = ''
                  }}
                />
                <ImagePlus size={22} />
                <strong>{referenceImages.length ? `已选择 ${referenceImages.length} 张` : '选择或拖入参考图'}</strong>
                <small>PNG / JPG / WEBP</small>
              </label>
              {referenceImages.length > 0 && (
                <div className="reference-grid" aria-label="已选择参考图">
                  {referenceImages.map((item) => (
                    <div className="reference-thumb" key={item.id}>
                      <img src={item.url} alt={item.file.name} />
                      <button type="button" className="reference-remove" onClick={() => removeReferenceImage(item.id)} title="删除参考图">
                        <X size={14} />
                      </button>
                      <span>{item.file.name}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <details className="advanced-attachments field full">
              <summary>高级附件 JSON</summary>
              <textarea
                className="compact-textarea"
                value={taskForm.attachments}
                onChange={(event) => setTaskForm({ ...taskForm, attachments: event.target.value })}
                placeholder="[]"
              />
            </details>
            <button type="submit" className="primary-action" disabled={busy['submit-task'] || enabledCookies.length === 0}>
              {busy['submit-task'] ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              提交生成
            </button>
          </form>

          <aside className="side-panel">
            <div className="section-title">
              <KeyRound size={18} />
              <h2>可用 Cookie</h2>
            </div>
            <CookieList
              cookies={cookies.slice(0, 6)}
              busy={busy}
              onTest={testCookie}
              onToggle={toggleCookie}
              onDelete={deleteCookie}
              compact
            />
          </aside>
        </section>
      )}

      {activeTab === 'cookies' && (
        <section className="manager-layout">
          <div className="tool-panel">
            <div className="section-title">
              <Upload size={18} />
              <h2>上传 Cookie</h2>
            </div>
            <div className="upload-row">
              <label className="field">
                <span>名称</span>
                <input value={cookieName} onChange={(event) => setCookieName(event.target.value)} placeholder="账号 A" />
              </label>
              <label className="file-button">
                <Upload size={16} />
                选择 JSON
                <input type="file" accept=".json,application/json" onChange={(event) => void uploadCookieFile(event.target.files?.[0] || null)} />
              </label>
            </div>
            <form className="paste-form" onSubmit={uploadPastedCookie}>
              <label className="field full">
                <span>粘贴 JSON</span>
                <textarea value={cookiePaste} onChange={(event) => setCookiePaste(event.target.value)} placeholder='[{"name":"...","value":"..."}]' />
              </label>
              <button type="submit" disabled={busy['cookie-paste']}>
                {busy['cookie-paste'] ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
                保存 Cookie
              </button>
            </form>
          </div>

          <div className="tool-panel">
            <div className="section-title spread">
              <span>
                <KeyRound size={18} />
                <h2>Cookie 池</h2>
              </span>
              <button className="secondary" onClick={checkAllCookies} disabled={busy['check-all']}>
                {busy['check-all'] ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                检测全部
              </button>
            </div>
            <CookieList cookies={cookies} busy={busy} onTest={testCookie} onToggle={toggleCookie} onDelete={deleteCookie} />
          </div>
        </section>
      )}

      {activeTab === 'tasks' && (
        <section className="tool-panel">
          <div className="section-title spread">
            <span>
              <Play size={18} />
              <h2>任务列表</h2>
            </span>
            <button className="secondary danger-text" onClick={clearSelectedTasks} disabled={busy['clear-tasks'] || selectedTaskIds.length === 0}>
              <Trash2 size={16} />
              删除所选{selectedTaskIds.length ? ` (${selectedTaskIds.length})` : ''}
            </button>
          </div>
          <div className="task-selection-bar">
            <button className="secondary" type="button" onClick={toggleVisibleTaskSelection} disabled={filteredTaskIds.length === 0}>
              {allVisibleTasksSelected ? <CheckSquare2 size={16} /> : <Square size={16} />}
              {allVisibleTasksSelected ? '取消全选' : '全选当前列表'}
            </button>
            <span>{selectedTaskIds.length ? `已选择 ${selectedTaskIds.length} 个任务` : '勾选任务后可批量删除'}</span>
          </div>
          <div className="task-filter-bar" aria-label="任务筛选">
            {(['all', 'running', 'submitted', 'success', 'failed'] as TaskStatusFilter[]).map((status) => {
              const count =
                status === 'all'
                  ? tasks.length
                  : status === 'running'
                    ? taskStats.running + taskStats.pending
                    : taskStats[status]
              return (
                <button
                  key={status}
                  type="button"
                  className={taskStatusFilter === status ? 'active' : ''}
                  onClick={() => setTaskStatusFilter(status)}
                >
                  {taskFilterLabels[status]}
                  <span>{count}</span>
                </button>
              )
            })}
          </div>
          <TaskList
            tasks={filteredTasks}
            busy={busy}
            selectedTaskIds={selectedTaskIds}
            onToggleTask={toggleTaskSelection}
            onRetry={retryTask}
            onDownloadOriginal={downloadOriginalVideo}
          />
        </section>
      )}

      {activeTab === 'settings' && (
        <section className="tool-panel runtime-panel">
          <div className="section-title">
            <Settings size={18} />
            <h2>运行时参数</h2>
          </div>
          <div className={`runtime-status ${runtimeConfig?.diagnostics.ready ? 'ready' : 'missing'}`}>
            {runtimeConfig?.diagnostics.ready
              ? '已自动补齐提交参数'
              : `缺少：${runtimeConfig?.diagnostics.missing.join(', ') || '未知'}`}
          </div>
          <form className="runtime-form" onSubmit={saveRuntimeConfig}>
            <label className="field full">
              <span>fp（自动，可选覆盖）</span>
              <input
                value={runtimeForm.fp}
                onChange={(event) => setRuntimeForm({ ...runtimeForm, fp: event.target.value })}
                placeholder="默认从 cookie 池的 s_v_web_id 或后端生成值取得"
              />
            </label>
            <label className="field full">
              <span>通用参数 JSON 或完整请求 URL（高级覆盖）</span>
              <textarea
                value={runtimeForm.common_params}
                onChange={(event) => setRuntimeForm({ ...runtimeForm, common_params: event.target.value })}
                placeholder='后端会自动填 aid/device_id/web_id/web_tab_id/fp'
              />
            </label>
            <button type="submit" className="primary-action" disabled={busy['runtime-config']}>
              {busy['runtime-config'] ? <Loader2 className="spin" size={16} /> : <Settings size={16} />}
              保存覆盖参数
            </button>
          </form>
        </section>
      )}
    </main>
  )
}

function StatCard({ label, value, detail, tone }: { label: string; value: number; detail: string; tone: string }) {
  return (
    <article className={`stat-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  )
}

function CookieList({
  cookies,
  busy,
  compact = false,
  onTest,
  onToggle,
  onDelete,
}: {
  cookies: CookieItem[]
  busy: Record<string, boolean>
  compact?: boolean
  onTest: (cookie: CookieItem) => void
  onToggle: (cookie: CookieItem) => void
  onDelete: (cookie: CookieItem) => void
}) {
  if (!cookies.length) {
    return <p className="empty">暂无 Cookie</p>
  }

  return (
    <div className={`cookie-list ${compact ? 'compact' : ''}`}>
      {cookies.map((cookie) => (
        <article className={`cookie-card ${cookie.enabled ? '' : 'disabled'}`} key={cookie.filename}>
          <div className="cookie-icon">
            <KeyRound size={18} />
          </div>
          <div className="cookie-main">
            <div className="cookie-name-row">
              <strong>{cookie.name}</strong>
              <span className={`status-badge ${cookie.enabled ? 'active' : 'disabled'}`}>
                {cookie.enabled ? '启用' : '停用'}
              </span>
            </div>
            <span>
              {cookie.remain_count ?? cookie.credits ?? '未知'} 次 · {formatSize(cookie.size)}
              {cookie.has_generating_task ? ' · 有生成任务' : ''}
            </span>
            {cookie.last_error && <small>{cookie.last_error}</small>}
          </div>
          <div className="row-actions">
            <button className="secondary" onClick={() => onTest(cookie)} disabled={busy[`test-${cookie.name}`]}>
              {busy[`test-${cookie.name}`] ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              检测
            </button>
            <button className="secondary" onClick={() => onToggle(cookie)} disabled={busy[`toggle-${cookie.name}`]}>
              {cookie.enabled ? '停用' : '启用'}
            </button>
            <button className="icon-button danger-text" onClick={() => onDelete(cookie)} title="删除">
              <Trash2 size={16} />
            </button>
          </div>
        </article>
      ))}
    </div>
  )
}

function TaskList({
  tasks,
  busy,
  selectedTaskIds,
  onToggleTask,
  onRetry,
  onDownloadOriginal,
}: {
  tasks: Task[]
  busy: Record<string, boolean>
  selectedTaskIds: string[]
  onToggleTask: (taskId: string) => void
  onRetry: (task: Task) => void
  onDownloadOriginal: (task: Task) => void
}) {
  if (!tasks.length) {
    return <p className="empty">暂无任务</p>
  }

  return (
    <div className="task-list">
      {tasks.map((task) => {
        const Icon = statusIcons[task.status] || AlertCircle
        const href = task.download_url ? `${API_BASE}${task.download_url}` : task.video_url || ''
        const canDownloadOriginal = task.status === 'submitted' || task.status === 'success' || Boolean(task.video_url)
        const selected = selectedTaskIds.includes(task.task_id)
        const attachments = task.attachments || []
        const visibleAttachments = attachments.slice(0, 3)
        const hiddenAttachmentCount = Math.max(0, (task.attachments_count || attachments.length) - visibleAttachments.length)
        return (
          <article className={`task-card ${task.status} ${selected ? 'selected' : ''}`} key={task.task_id}>
            <button
              type="button"
              className="task-select-button"
              onClick={() => onToggleTask(task.task_id)}
              aria-label={selected ? '取消选择任务' : '选择任务'}
              title={selected ? '取消选择任务' : '选择任务'}
            >
              {selected ? <CheckSquare2 size={18} /> : <Square size={18} />}
            </button>
            <div className="task-status">
              <Icon className={task.status === 'running' ? 'spin' : ''} size={18} />
            </div>
            <div className="task-main">
              <div className="task-title-row">
                <strong>{statusLabels[task.status] || task.status}</strong>
                <span>{task.ratio} · {task.model}</span>
              </div>
              <p>{task.prompt}</p>
              {visibleAttachments.length > 0 && (
                <div className="task-materials" aria-label="任务素材">
                  {visibleAttachments.map((attachment) => (
                    <a
                      className="task-material"
                      href={attachment.url ? `${API_BASE}${attachment.url}` : undefined}
                      target="_blank"
                      rel="noreferrer"
                      key={`${task.task_id}-${attachment.index}`}
                      title={attachment.fileName}
                    >
                      {attachment.url ? (
                        <img src={`${API_BASE}${attachment.url}`} alt={attachment.fileName} loading="lazy" />
                      ) : (
                        <ImagePlus size={18} />
                      )}
                      <span>
                        <strong>{attachment.fileName}</strong>
                        <small>{formatAttachmentMeta(attachment)}</small>
                      </span>
                    </a>
                  ))}
                  {hiddenAttachmentCount > 0 && <span className="task-material-more">+{hiddenAttachmentCount}</span>}
                </div>
              )}
              <div className="task-meta">
                <span>{task.cookie_name || task.cookie_file || '自动轮询'}</span>
                <span>{formatTime(task.created_at)}</span>
                {task.error_message && <span className="error-text">{task.error_message}</span>}
              </div>
              <div className="progress-track">
                <div style={{ width: `${Math.max(4, Math.min(100, task.progress || 0))}%` }} />
              </div>
            </div>
            <div className="row-actions">
              {canDownloadOriginal && (
                <button className="secondary" onClick={() => onDownloadOriginal(task)} disabled={busy[`original-${task.task_id}`]}>
                  {busy[`original-${task.task_id}`] ? <Loader2 className="spin" size={16} /> : <Download size={16} />}
                  补抓视频
                </button>
              )}
              {task.status === 'failed' && (
                <button className="secondary" onClick={() => onRetry(task)} disabled={busy[`retry-${task.task_id}`]}>
                  {busy[`retry-${task.task_id}`] ? <Loader2 className="spin" size={16} /> : <RotateCcw size={16} />}
                  重新提交
                </button>
              )}
              {href && (
                <a className="secondary download-action" href={href} target="_blank" rel="noreferrer">
                  <Download size={16} />
                  视频
                </a>
              )}
            </div>
          </article>
        )
      })}
    </div>
  )
}

async function api<T = unknown>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path.startsWith('http') ? path : `${API_BASE}${path}`, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = data?.error?.message || data?.message || `HTTP ${response.status}`
    throw new Error(message)
  }
  return data as T
}

function readError(error: unknown) {
  return error instanceof Error ? error.message : '操作失败'
}

function formatSize(size: number) {
  if (!Number.isFinite(size) || size <= 0) return '0 KB'
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function formatAttachmentMeta(attachment: TaskAttachment) {
  const dimensions = attachment.width && attachment.height ? `${attachment.width}×${attachment.height}` : ''
  const size = attachment.size ? formatSize(attachment.size) : ''
  return [dimensions, size].filter(Boolean).join(' · ') || attachment.type || 'material'
}

function formatTime(value?: string | null) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

export default App

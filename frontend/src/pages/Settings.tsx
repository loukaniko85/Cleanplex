import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { CheckCircle2, XCircle, Loader2, Eye, EyeOff, RotateCcw, Plus, Play, Pause, Trash2, AlertTriangle } from 'lucide-react'

interface Settings {
  plex_url: string
  plex_token: string
  poll_interval: string
  confidence_threshold: string
  skip_buffer_ms: string
  scan_step_ms: string
  scan_workers: string
  nudenet_model: string
  nudenet_model_path: string
  segment_gap_ms: string
  segment_min_hits: string
  scan_window_start: string
  scan_window_end: string
  log_level: string
  excluded_library_ids: string
  scan_ratings: string
  scan_labels: string
  videoskip_auto_fetch: string
  videoskip_auto_daily_limit: string
  videoskip_auto_delay_ms: string
  videoskip_auto_sweep_minutes: string
  videoskip_auto_max_attempts: string
  videoskip_auto_libraries: string
  videoskip_auto_respect_scan_window: string
}

interface QueueRecent {
  title: string
  status: string
  skip_id: string
  imported: number
  last_error: string
  updated_at: string
}

interface WebhookEvent {
  received_at: string
  event: string
  rating_key: string
  title: string
  result: string
  detail: string
}

interface WebhookStatus {
  total: number
  last_received_at: string | null
  auto_fetch: boolean
  token_required: boolean
  events: WebhookEvent[]
}

interface QueueStatus {
  auto_fetch: boolean
  paused: boolean
  queued: number
  running: number
  done: number
  no_match: number
  failed: number
  skipped: number
  total: number
  daily_used: number
  daily_limit: number
  current: { plex_guid: string; title: string } | null
  recent: QueueRecent[]
}

interface Library {
  id: string
  title: string
  type: string
}

interface LibraryCoverage {
  library_id: string
  library_title: string
  total: number
  with_segments: number
  with_videoskip: number
  without_segments: number
  without_videoskip: number
}

interface VideoskipCoverage {
  total: number
  with_segments: number
  with_videoskip: number
  without_segments: number
  without_videoskip: number
  libraries: LibraryCoverage[]
}

interface ScanLibraryCoverage {
  library_id: string
  library_title: string
  total: number
  scanned: number
  with_segments: number
  with_nudenet: number
  without_segments: number
  without_nudenet: number
}

interface ScanCoverage {
  total: number
  scanned: number
  with_segments: number
  with_nudenet: number
  without_segments: number
  without_nudenet: number
  libraries: ScanLibraryCoverage[]
}

const DEFAULT: Settings = {
  plex_url: '',
  plex_token: '',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  nudenet_model: '320n',
  nudenet_model_path: '',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED","FEMALE_GENITALIA_EXPOSED","MALE_GENITALIA_EXPOSED","ANUS_EXPOSED","BUTTOCKS_EXPOSED"]',
  videoskip_auto_fetch: '0',
  videoskip_auto_daily_limit: '50',
  videoskip_auto_delay_ms: '1500',
  videoskip_auto_sweep_minutes: '60',
  videoskip_auto_max_attempts: '2',
  videoskip_auto_libraries: '[]',
  videoskip_auto_respect_scan_window: '0',
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-300 mb-1">{label}</label>
      {children}
      {hint && <p className="text-xs text-gray-600 mt-1">{hint}</p>}
    </div>
  )
}

export default function SettingsPage() {
  const [form, setForm] = useState<Settings>(DEFAULT)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [validatingModel, setValidatingModel] = useState(false)
  const [modelValidationResult, setModelValidationResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [showToken, setShowToken] = useState(false)
  const [libraries, setLibraries] = useState<Library[]>([])
  const [detectorLabels, setDetectorLabels] = useState<string[]>([])
  const [coverage, setCoverage] = useState<VideoskipCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(true)
  const [scanCoverage, setScanCoverage] = useState<ScanCoverage | null>(null)
  const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null)
  const [queueLoading, setQueueLoading] = useState(true)
  const [webhookStatus, setWebhookStatus] = useState<WebhookStatus | null>(null)
  const [webhookLoading, setWebhookLoading] = useState(true)
  const [webhookCopied, setWebhookCopied] = useState(false)
  const [queueAction, setQueueAction] = useState('')
  const [queueMessage, setQueueMessage] = useState<{ ok: boolean; message: string } | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [restarted, setRestarted] = useState(false)
  const [loadError, setLoadError] = useState(false)
  const [saveError, setSaveError] = useState('')

  const load = useCallback(() => {
    setLoading(true)
    setLoadError(false)
    Promise.all([
      api.get<Settings>('/api/settings'),
      api.get<{ libraries: Library[] }>('/api/libraries').catch(() => ({ libraries: [] })),
      api.get<{ labels: string[] }>('/api/settings/detector-labels').catch(() => ({ labels: [] })),
      api.get<VideoskipCoverage>('/api/videoskip/coverage').catch(() => null),
      api.get<ScanCoverage>('/api/scan/coverage').catch(() => null),
      api.get<QueueStatus>('/api/videoskip/queue').catch(() => null),
      api.get<WebhookStatus>('/api/webhooks/plex/status').catch(() => null),
    ])
      .then(([settings, libs, labels, cov, scanCov, queue, webhook]) => {
        setForm({ ...DEFAULT, ...settings })
        setLibraries(libs.libraries)
        setDetectorLabels(labels.labels)
        setCoverage(cov)
        setScanCoverage(scanCov)
        setQueueStatus(queue)
        setWebhookStatus(webhook)
      })
      .catch(() => {
        // Only /api/settings can reject here (the rest carry their own catch
        // fallbacks) — without it there is no form to render.
        setLoadError(true)
      })
      .finally(() => {
        setLoading(false)
        setCoverageLoading(false)
        setQueueLoading(false)
        setWebhookLoading(false)
      })
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const excludedIds: string[] = (() => {
    try { return JSON.parse(form.excluded_library_ids) } catch { return [] }
  })()

  const toggleExcluded = (id: string) => {
    const next = excludedIds.includes(id)
      ? excludedIds.filter(x => x !== id)
      : [...excludedIds, id]
    setForm(f => ({ ...f, excluded_library_ids: JSON.stringify(next) }))
  }

  const autoLibraries: string[] = (() => {
    try { return JSON.parse(form.videoskip_auto_libraries) } catch { return [] }
  })()

  const toggleAutoLibrary = (id: string) => {
    const next = autoLibraries.includes(id)
      ? autoLibraries.filter(x => x !== id)
      : [...autoLibraries, id]
    setForm(f => ({ ...f, videoskip_auto_libraries: JSON.stringify(next) }))
  }

  const set = (k: keyof Settings) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm(f => ({ ...f, [k]: e.target.value }))

  const save = async () => {
    setSaving(true)
    setSaved(false)
    // Cleared here so a previous failure does not linger past a success.
    setSaveError('')
    try {
      await api.put('/api/settings', form)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e: any) {
      setSaveError(e?.message ?? 'Saving settings failed.')
    } finally {
      setSaving(false)
    }
  }

  const testConnection = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      // Save first so the server uses current values
      await api.put('/api/settings', { plex_url: form.plex_url, plex_token: form.plex_token })
      const r = await api.post<{ ok: boolean; message: string }>('/api/settings/test-connection')
      setTestResult(r)
    } catch (e: any) {
      setTestResult({ ok: false, message: e?.message ?? 'Connection test failed.' })
    } finally {
      setTesting(false)
    }
  }

  const refreshCoverage = async () => {
    setCoverageLoading(true)
    try {
      const [cov, scanCov] = await Promise.all([
        api.get<VideoskipCoverage>('/api/videoskip/coverage'),
        api.get<ScanCoverage>('/api/scan/coverage').catch(() => null),
      ])
      setCoverage(cov)
      setScanCoverage(scanCov)
    } catch {
      setQueueMessage({ ok: false, message: 'Could not load coverage.' })
    } finally {
      setCoverageLoading(false)
    }
  }

  const refreshQueue = async () => {
    setQueueLoading(true)
    try {
      setQueueStatus(await api.get<QueueStatus>('/api/videoskip/queue'))
    } catch {
      setQueueMessage({ ok: false, message: 'Could not load the VideoSkip queue.' })
    } finally {
      setQueueLoading(false)
    }
  }

  const refreshWebhook = async () => {
    setWebhookLoading(true)
    try {
      setWebhookStatus(await api.get<WebhookStatus>('/api/webhooks/plex/status'))
    } catch {
      setQueueMessage({ ok: false, message: 'Could not load webhook status.' })
    } finally {
      setWebhookLoading(false)
    }
  }

  const webhookUrl = `${window.location.origin}/api/webhooks/plex`

  const copyWebhook = async () => {
    const url = webhookStatus?.token_required
      ? `${webhookUrl}?token=<CLEANPLEX_WEBHOOK_TOKEN>`
      : webhookUrl
    try {
      await navigator.clipboard.writeText(url)
      setWebhookCopied(true)
      setTimeout(() => setWebhookCopied(false), 2000)
    } catch {
      setQueueMessage({ ok: false, message: 'Clipboard unavailable; select the URL manually.' })
    }
  }

  const runQueueAction = async (
    action: string,
    fn: () => Promise<{ ok: boolean; message: string }>,
  ) => {
    setQueueAction(action)
    setQueueMessage(null)
    try {
      setQueueMessage(await fn())
      await Promise.all([refreshQueue(), refreshCoverage()])
    } catch (e: any) {
      setQueueMessage({ ok: false, message: e?.message ?? 'Queue action failed.' })
    } finally {
      setQueueAction('')
    }
  }

  const queueMissing = () =>
    runQueueAction('enqueue', async () => {
      const r = await api.post<{ queued: number; candidates: number }>(
        '/api/videoskip/queue/enqueue',
        { limit: 50 },
      )
      return {
        ok: r.queued > 0,
        message: `Queued ${r.queued} of ${r.candidates} candidate(s).`,
      }
    })

  const toggleQueuePause = () =>
    runQueueAction('pause', async () => {
      const pause = !queueStatus?.paused
      await api.post(`/api/videoskip/queue/${pause ? 'pause' : 'resume'}`)
      return { ok: true, message: pause ? 'Queue paused.' : 'Queue resumed.' }
    })

  const clearQueue = () =>
    runQueueAction('clear', async () => {
      const r = await api.post<{ deleted: number }>('/api/videoskip/queue/clear')
      return { ok: true, message: `Cleared ${r.deleted} finished item(s).` }
    })

  const retryFailed = () =>
    runQueueAction('retry', async () => {
      const r = await api.post<{ queued: number }>('/api/videoskip/queue/retry-failed')
      return { ok: true, message: `Requeued ${r.queued} failed item(s).` }
    })

  const validateModelPath = async () => {
    setValidatingModel(true)
    setModelValidationResult(null)
    try {
      const r = await api.post<{ ok: boolean; message: string }>('/api/settings/validate-model-path', {
        nudenet_model: form.nudenet_model,
        nudenet_model_path: form.nudenet_model_path,
      })
      setModelValidationResult(r)
    } catch (e) {
      setModelValidationResult({ ok: false, message: 'Validation request failed' })
    } finally {
      setValidatingModel(false)
    }
  }

  const restartScanner = async () => {
    setRestarting(true)
    setRestarted(false)
    try {
      await api.post('/api/scan/restart-scanner')
      setRestarted(true)
      setTimeout(() => setRestarted(false), 5000)
    } finally {
      setRestarting(false)
    }
  }

  const inputCls = "w-full px-3 py-2 bg-plex-darker border border-plex-border rounded-lg text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-plex-orange/60 transition-colors"

  if (loading) return <div className="text-gray-500 text-sm">Loading...</div>

  if (loadError)
    return (
      <div className="max-w-xl space-y-4">
        <h1 className="text-2xl font-bold text-gray-100">Settings</h1>
        <p className="flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={15} /> Could not load settings. The API may be down.
        </p>
        <button
          onClick={load}
          className="px-4 py-2 text-sm bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors flex items-center gap-2"
        >
          <RotateCcw size={14} /> Retry
        </button>
      </div>
    )

  return (
    <div className="max-w-xl space-y-8">
      <h1 className="text-2xl font-bold text-gray-100">Settings</h1>

      {/* Plex connection */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Plex Connection</h2>
        <div className="space-y-4">
          <Field label="Plex Server URL" hint="e.g. http://192.168.1.10:32400">
            <input
              type="url"
              value={form.plex_url}
              onChange={set('plex_url')}
              placeholder="http://localhost:32400"
              className={inputCls}
            />
          </Field>

          <Field label="Plex Token" hint="Find yours at plex.tv/devices.xml or in Plex logs">
            <div className="relative">
              <input
                type={showToken ? 'text' : 'password'}
                value={form.plex_token}
                onChange={set('plex_token')}
                placeholder="xxxxxxxxxxxxxxxxxxxx"
                className={inputCls + ' pr-10'}
              />
              <button
                type="button"
                onClick={() => setShowToken(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
              >
                {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </Field>

          <div className="flex items-center gap-3">
            <button
              onClick={testConnection}
              disabled={testing || !form.plex_url || !form.plex_token}
              className="px-4 py-2 text-sm bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
            >
              {testing && <Loader2 size={14} className="animate-spin" />}
              Test Connection
            </button>
            {testResult && (
              <span className={`flex items-center gap-1.5 text-sm ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                {testResult.ok ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
                {testResult.message}
              </span>
            )}
          </div>
        </div>
      </section>

      {/* Filter settings */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Filter Settings</h2>
        <div className="space-y-4">
          <Field label="Poll Interval (seconds)" hint="How often to check active streams for scenes to skip">
            <input type="number" min="2" max="30" value={form.poll_interval} onChange={set('poll_interval')} className={inputCls} />
          </Field>
          <Field label="Detection Confidence Threshold" hint="Frames scoring above this value (0–1) are flagged as nudity. Lower = more sensitive.">
            <input type="number" min="0.1" max="1" step="0.05" value={form.confidence_threshold} onChange={set('confidence_threshold')} className={inputCls} />
          </Field>
          <Field label="Scan Frame Interval (ms)" hint="How often frames are sampled during scanning. Lower catches more scenes but takes longer.">
            <input type="number" min="1000" max="20000" step="500" value={form.scan_step_ms} onChange={set('scan_step_ms')} className={inputCls} />
          </Field>
          <Field label="Scanner Workers" hint="How many titles can be scanned in parallel. Higher values use more CPU, disk, and memory.">
            <input type="number" min="1" max="12" step="1" value={form.scan_workers} onChange={set('scan_workers')} className={inputCls} />
          </Field>
          <Field label="NudeNet Model" hint="320n is bundled and fastest. 640m is downloaded by Cleanplex automatically and then cached locally.">
            <select value={form.nudenet_model} onChange={set('nudenet_model')} className={inputCls}>
              <option value="320n">320n (default, fast)</option>
              <option value="640m">640m (higher accuracy, slower)</option>
            </select>
          </Field>
          <Field label="Prepare 640m Model" hint="Optional: click once to pre-download 640m now. Otherwise it will auto-download on first 640m scan.">
            <div className="mt-2 flex items-center gap-3">
              <button
                type="button"
                onClick={validateModelPath}
                disabled={validatingModel}
                className="px-3 py-2 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-2"
              >
                {validatingModel && <Loader2 size={13} className="animate-spin" />}
                Download/Check 640m model
              </button>
              {modelValidationResult && (
                <span className={`flex items-center gap-1.5 text-xs ${modelValidationResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                  {modelValidationResult.ok ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                  {modelValidationResult.message}
                </span>
              )}
            </div>
          </Field>
          <Field label="Segment Merge Gap (ms)" hint="Flagged frames closer than this are merged into one segment.">
            <input type="number" min="1000" max="30000" step="500" value={form.segment_gap_ms} onChange={set('segment_gap_ms')} className={inputCls} />
          </Field>
          <Field label="Minimum Hits Per Segment" hint="Require at least this many flagged frames in a cluster to keep a segment. Increase to reduce false positives.">
            <input type="number" min="1" max="6" step="1" value={form.segment_min_hits} onChange={set('segment_min_hits')} className={inputCls} />
          </Field>
          <Field label="Skip Buffer (ms)" hint="Extra milliseconds to seek past the end of a detected segment">
            <input type="number" min="0" step="500" value={form.skip_buffer_ms} onChange={set('skip_buffer_ms')} className={inputCls} />
          </Field>
        </div>
      </section>

      {/* Scan schedule */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Scan Schedule</h2>
        <p className="text-xs text-gray-500 mb-4">Background video scanning only runs within this time window to avoid slowing your device during the day.</p>
        <div className="flex gap-4">
          <Field label="Start Time">
            <input type="time" value={form.scan_window_start} onChange={set('scan_window_start')} className={inputCls} />
          </Field>
          <Field label="End Time">
            <input type="time" value={form.scan_window_end} onChange={set('scan_window_end')} className={inputCls} />
          </Field>
        </div>
      </section>

      {/* Scan ratings */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Scan Ratings</h2>
        <p className="text-xs text-gray-500 mb-3">Only scan titles with these content ratings. Leave all unchecked to scan everything.</p>
        <div className="grid grid-cols-2 gap-2">
          {/* VALUE is the exact content_rating string stored in Plex / the DB.
              Empty string ("") represents titles Plex left unrated. */}
          {[
            { label: 'G',            value: 'G' },
            { label: 'PG',           value: 'PG' },
            { label: 'PG-13',        value: 'PG-13' },
            { label: 'R',            value: 'R' },
            { label: 'NC-17',        value: 'NC-17' },
            { label: 'TV-G',         value: 'TV-G' },
            { label: 'TV-PG',        value: 'TV-PG' },
            { label: 'TV-14',        value: 'TV-14' },
            { label: 'TV-MA',        value: 'TV-MA' },
            { label: 'NR',           value: 'NR' },
            { label: 'X',            value: 'X' },
            { label: 'Unrated (no rating set in Plex)', value: '' },
          ].map(({ label, value }) => {
            const selected: string[] = (() => { try { return JSON.parse(form.scan_ratings) } catch { return [] } })()
            const checked = selected.includes(value)
            const toggle = () => {
              const next = checked ? selected.filter((r: string) => r !== value) : [...selected, value]
              setForm(f => ({ ...f, scan_ratings: JSON.stringify(next) }))
            }
            return (
              <label key={value === '' ? '__unrated__' : value} className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-plex-orange" />
                <span className={`text-sm ${checked ? 'text-gray-100' : 'text-gray-500'}`}>{label}</span>
              </label>
            )
          })}
        </div>
      </section>

      {/* Detector labels */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Detector Labels</h2>
        <p className="text-xs text-gray-500 mb-3">Choose which NudeNet labels should count toward detection. Exposed-only labels are usually less noisy.</p>
        <div className="grid grid-cols-2 gap-2">
          {detectorLabels.map(label => {
            const selected = (() => { try { return JSON.parse(form.scan_labels) } catch { return [] } })()
            const checked = selected.includes(label)
            const toggle = () => {
              const next = checked ? selected.filter((r: string) => r !== label) : [...selected, label]
              setForm(f => ({ ...f, scan_labels: JSON.stringify(next) }))
            }
            return (
              <label key={label} className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={checked} onChange={toggle} className="w-4 h-4 accent-plex-orange" />
                <span className={`text-sm ${checked ? 'text-gray-100' : 'text-gray-500'}`}>{label}</span>
              </label>
            )
          })}
        </div>
      </section>

      {/* Library exclusions */}
      {libraries.length > 0 && (
        <section>
          <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Library Exclusions</h2>
          <p className="text-xs text-gray-500 mb-3">Excluded libraries will not be scanned.</p>
          <div className="space-y-2">
            {libraries.map(lib => (
              <label key={lib.id} className="flex items-center gap-3 cursor-pointer group">
                <input
                  type="checkbox"
                  checked={excludedIds.includes(lib.id)}
                  onChange={() => toggleExcluded(lib.id)}
                  className="w-4 h-4 accent-plex-orange"
                />
                <span className={`text-sm ${excludedIds.includes(lib.id) ? 'text-gray-500 line-through' : 'text-gray-200'}`}>
                  {lib.title}
                </span>
                <span className="text-xs text-gray-600">{lib.type}</span>
              </label>
            ))}
          </div>
        </section>
      )}

      {/* Local scan (NudeNet) coverage */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">Local scanning (NudeNet)</h2>
        <p className="text-xs text-gray-500 mb-4">
          Frame-by-frame nudity detection runs locally on the Plex host, so this coverage is
          independent of any import. "Scanned" counts titles the scanner finished; "From NudeNet"
          counts titles that actually own ML-produced segments.
        </p>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
          {[
            { label: 'Titles', value: scanCoverage?.total ?? 0, tone: 'text-gray-100' },
            { label: 'Scanned', value: scanCoverage?.scanned ?? 0, tone: 'text-green-400' },
            { label: 'From NudeNet', value: scanCoverage?.with_nudenet ?? 0, tone: 'text-blue-400' },
            { label: 'No NudeNet', value: scanCoverage?.without_nudenet ?? 0, tone: 'text-amber-400' },
          ].map(card => (
            <div key={card.label} className="bg-plex-card border border-plex-border rounded-lg px-3 py-2">
              <p className="text-[11px] text-gray-500">{card.label}</p>
              <p className={`text-xl font-semibold ${card.tone}`}>
                {coverageLoading ? '—' : card.value}
              </p>
            </div>
          ))}
        </div>

        {scanCoverage && scanCoverage.libraries.length > 0 && (
          <div className="mb-4 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-gray-500">
                <tr>
                  <th className="text-left font-medium pb-1">Library</th>
                  <th className="text-right font-medium pb-1">Titles</th>
                  <th className="text-right font-medium pb-1">Scanned</th>
                  <th className="text-right font-medium pb-1">NudeNet</th>
                  <th className="text-right font-medium pb-1">No NudeNet</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {scanCoverage.libraries.map(lib => (
                  <tr key={lib.library_id || lib.library_title} className="border-t border-plex-border/50">
                    <td className="py-1.5 pr-2 text-gray-200">{lib.library_title}</td>
                    <td className="text-right py-1.5 px-1">{lib.total}</td>
                    <td className="text-right py-1.5 px-1 text-green-400">{lib.scanned}</td>
                    <td className="text-right py-1.5 px-1 text-blue-400">{lib.with_nudenet}</td>
                    <td className="text-right py-1.5 px-1 text-amber-400">{lib.without_nudenet}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* VideoSkip Exchange coverage */}
      <section>
        <h2 className="text-base font-semibold text-gray-200 mb-4 pb-2 border-b border-plex-border">VideoSkip Exchange</h2>
        <p className="text-xs text-gray-500 mb-4">
          Community-timed skip files from videoskip.org, matched by IMDb ID or title + year /
          show + season + episode. Lookups are rate-limited; see the{' '}
          <Link to="/videoskip" className="text-plex-orange hover:underline">
            VideoSkip page
          </Link>{' '}
          for live coverage, queue history and Exchange search.
        </p>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
          {[
            { label: 'Titles', value: coverage?.total ?? 0, tone: 'text-gray-100' },
            { label: 'Have segments', value: coverage?.with_segments ?? 0, tone: 'text-green-400' },
            { label: 'From VideoSkip', value: coverage?.with_videoskip ?? 0, tone: 'text-blue-400' },
            { label: 'No VideoSkip', value: coverage?.without_videoskip ?? 0, tone: 'text-amber-400' },
          ].map(card => (
            <div key={card.label} className="bg-plex-card border border-plex-border rounded-lg px-3 py-2">
              <p className="text-[11px] text-gray-500">{card.label}</p>
              <p className={`text-xl font-semibold ${card.tone}`}>
                {coverageLoading ? '—' : card.value}
              </p>
            </div>
          ))}
        </div>

        {coverage && coverage.libraries.length > 0 && (
          <div className="mb-4 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-gray-500">
                <tr>
                  <th className="text-left font-medium pb-1">Library</th>
                  <th className="text-right font-medium pb-1">Titles</th>
                  <th className="text-right font-medium pb-1">Segments</th>
                  <th className="text-right font-medium pb-1">VideoSkip</th>
                  <th className="text-right font-medium pb-1">No VideoSkip</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {coverage.libraries.map(lib => (
                  <tr key={lib.library_id || lib.library_title} className="border-t border-plex-border/50">
                    <td className="py-1.5 pr-2 text-gray-200">{lib.library_title}</td>
                    <td className="text-right py-1.5 px-1">{lib.total}</td>
                    <td className="text-right py-1.5 px-1 text-green-400">{lib.with_segments}</td>
                    <td className="text-right py-1.5 px-1 text-blue-400">{lib.with_videoskip}</td>
                    <td className="text-right py-1.5 px-1 text-amber-400">{lib.without_videoskip}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="space-y-4 mb-4">
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={form.videoskip_auto_fetch === '1'}
              onChange={e =>
                setForm(f => ({
                  ...f,
                  videoskip_auto_fetch: e.target.checked ? '1' : '0',
                }))
              }
              className="w-4 h-4 accent-plex-orange mt-0.5"
            />
            <span>
              <span className="text-sm text-gray-300">
                Automatically fetch new and missing titles
              </span>
              <span className="block text-xs text-gray-600">
                Queues titles without VideoSkip segments when they are discovered and on each
                sweep. The worker handles one title at a time with a daily ceiling. Saved with
                the main Save Settings button below.
              </span>
            </span>
          </label>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Field label="Daily limit" hint="0 = unlimited">
              <input
                type="number"
                min={0}
                value={form.videoskip_auto_daily_limit}
                onChange={set('videoskip_auto_daily_limit')}
                className={inputCls}
              />
            </Field>
            <Field label="Delay (ms)" hint="Between titles">
              <input
                type="number"
                min={0}
                value={form.videoskip_auto_delay_ms}
                onChange={set('videoskip_auto_delay_ms')}
                className={inputCls}
              />
            </Field>
            <Field label="Sweep (minutes)" hint="0 = never">
              <input
                type="number"
                min={0}
                value={form.videoskip_auto_sweep_minutes}
                onChange={set('videoskip_auto_sweep_minutes')}
                className={inputCls}
              />
            </Field>
            <Field label="Max attempts" hint="Per title">
              <input
                type="number"
                min={1}
                value={form.videoskip_auto_max_attempts}
                onChange={set('videoskip_auto_max_attempts')}
                className={inputCls}
              />
            </Field>
          </div>

          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Libraries" hint="None checked = every library.">
              <div className="space-y-1.5">
                {libraries.map(lib => (
                  <label key={lib.id} className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={autoLibraries.includes(lib.id)}
                      onChange={() => toggleAutoLibrary(lib.id)}
                      className="w-4 h-4 accent-plex-orange"
                    />
                    <span className="text-sm text-gray-300">{lib.title}</span>
                    <span className="text-xs text-gray-600">{lib.type}</span>
                  </label>
                ))}
                {libraries.length === 0 && (
                  <p className="text-xs text-gray-600">No Plex libraries loaded yet.</p>
                )}
              </div>
            </Field>

            <label className="flex items-start gap-3 cursor-pointer self-start">
              <input
                type="checkbox"
                checked={form.videoskip_auto_respect_scan_window === '1'}
                onChange={e =>
                  setForm(f => ({
                    ...f,
                    videoskip_auto_respect_scan_window: e.target.checked ? '1' : '0',
                  }))
                }
                className="w-4 h-4 accent-plex-orange mt-0.5"
              />
              <span>
                <span className="text-sm text-gray-300">Only run during the scan window</span>
                <span className="block text-xs text-gray-600">
                  Reuses the scanning window below. Manual queueing still runs at any time.
                </span>
              </span>
            </label>
          </div>
        </div>

        <div className="bg-plex-card border border-plex-border rounded-lg p-3 space-y-2 mb-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={queueStatus?.paused ? 'text-amber-400' : 'text-green-400'}>
              {queueStatus?.paused ? 'Paused' : 'Worker active'}
            </span>
            <span className="text-gray-600">·</span>
            <span className="text-gray-400">{queueStatus?.queued ?? 0} queued</span>
            <span className="text-gray-400">{queueStatus?.running ?? 0} running</span>
            <span className="text-gray-400">{queueStatus?.done ?? 0} done</span>
            <span className="text-gray-400">{queueStatus?.no_match ?? 0} no match</span>
            <span className="text-gray-400">{queueStatus?.failed ?? 0} failed</span>
            <span className="ml-auto text-gray-500">
              {queueStatus?.daily_used ?? 0}/{queueStatus?.daily_limit || '∞'} today
            </span>
            <button
              type="button"
              onClick={refreshQueue}
              disabled={queueLoading}
              title="Refresh queue"
              className="p-1 text-gray-500 hover:text-gray-200 disabled:opacity-40"
            >
              <RotateCcw size={12} className={queueLoading ? 'animate-spin' : ''} />
            </button>
          </div>

          {queueStatus?.current && (
            <p className="text-xs text-plex-orange truncate">
              Now: {queueStatus.current.title}
            </p>
          )}
          {queueMessage && (
            <p
              className={`text-xs flex items-center gap-1.5 ${
                queueMessage.ok ? 'text-green-400' : 'text-red-400'
              }`}
            >
              {queueMessage.ok ? <CheckCircle2 size={12} /> : <XCircle size={12} />}
              {queueMessage.message}
            </p>
          )}

          <div className="flex flex-wrap gap-2 pt-2 border-t border-plex-border/50">
            <button
              type="button"
              onClick={queueMissing}
              disabled={!!queueAction || (coverage?.without_videoskip ?? 0) === 0}
              className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-1.5"
            >
              {queueAction === 'enqueue' ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Plus size={13} />
              )}
              Queue missing (50)
            </button>
            <button
              type="button"
              onClick={toggleQueuePause}
              disabled={!!queueAction}
              className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-1.5"
            >
              {queueStatus?.paused ? <Play size={13} /> : <Pause size={13} />}
              {queueStatus?.paused ? 'Resume' : 'Pause'}
            </button>
            <button
              type="button"
              onClick={retryFailed}
              disabled={!!queueAction || (queueStatus?.failed ?? 0) === 0}
              className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-1.5"
            >
              <RotateCcw size={13} /> Retry failed
            </button>
            <button
              type="button"
              onClick={clearQueue}
              disabled={!!queueAction || (queueStatus?.total ?? 0) === 0}
              className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-red-400/50 hover:text-red-300 transition-colors disabled:opacity-40 flex items-center gap-1.5"
            >
              <Trash2 size={13} /> Clear finished
            </button>
            <button
              type="button"
              onClick={refreshCoverage}
              disabled={coverageLoading}
              className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:border-gray-500 hover:text-white transition-colors disabled:opacity-40 flex items-center gap-1.5 ml-auto"
            >
              <RotateCcw size={13} className={coverageLoading ? 'animate-spin' : ''} /> Coverage
            </button>
          </div>
        </div>

        <div className="rounded-xl border border-plex-border bg-black/15 p-3 space-y-2 mb-3">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-xs font-semibold text-gray-200">Plex webhook</h3>
            <span className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] text-gray-500">
              {webhookStatus?.token_required ? 'token required' : 'no token'}
            </span>
            <span className="text-[11px] text-gray-500">
              {webhookStatus?.total ?? 0} delivery
              {(webhookStatus?.total ?? 0) === 1 ? '' : 'ies'} received
            </span>
            <button
              type="button"
              onClick={refreshWebhook}
              disabled={webhookLoading}
              title="Refresh webhook status"
              className="ml-auto p-1 text-gray-500 hover:text-gray-200 disabled:opacity-40"
            >
              <RotateCcw size={12} className={webhookLoading ? 'animate-spin' : ''} />
            </button>
          </div>

          <p className="text-[11px] text-gray-600">
            Add this URL in Plex → Settings → Webhooks. Plex fires{' '}
            <code>library.new</code> when media is added, not when playback starts.
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded border border-plex-border bg-plex-bg px-2 py-1.5 text-[11px] text-gray-300">
              {webhookUrl}
              {webhookStatus?.token_required ? '?token=…' : ''}
            </code>
            <button
              type="button"
              onClick={copyWebhook}
              className="rounded border border-plex-border bg-plex-card px-2 py-1.5 text-[11px] text-gray-400 hover:text-white"
            >
              {webhookCopied ? 'Copied' : 'Copy'}
            </button>
          </div>

          {webhookStatus?.last_received_at ? (
            <p className="text-[11px] text-green-400">
              Last delivery{' '}
              {new Date(
                webhookStatus.last_received_at.replace(' ', 'T') + 'Z',
              ).toLocaleString()}
              {webhookStatus.events[0] && (
                <> · {webhookStatus.events[0].event || 'unknown'} · {webhookStatus.events[0].result}</>
              )}
            </p>
          ) : (
            <p className="text-[11px] text-amber-400">
              No deliveries yet. Add the URL above, then add a movie to Plex (or use the
              manual curl example in the README).
            </p>
          )}

          {webhookStatus?.events.slice(0, 3).map((event, index) => (
            <div
              key={`${event.received_at}-${index}`}
              className="flex items-center gap-2 rounded bg-black/20 px-2 py-1 text-[10px]"
            >
              <span className="w-16 flex-shrink-0 text-gray-600">
                {event.event || 'invalid'}
              </span>
              <span className="min-w-0 flex-1 truncate text-gray-400">
                {event.title || event.rating_key || '—'}
              </span>
              <span className="text-gray-500">{event.result}</span>
              {event.detail && (
                <span title={event.detail} className="text-gray-600">
                  <AlertTriangle size={10} />
                </span>
              )}
            </div>
          ))}
        </div>

        <p className="text-xs text-gray-600">
          “Queue missing” adds titles without VideoSkip data alongside any existing scanner
          segments. Use a title's VideoSkip tab with “Replace existing” to swap sources. The
          worker is single-threaded and serialized to respect the Exchange.
        </p>
      </section>

      {/* Save */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={save}
          disabled={saving}
          className="px-6 py-2.5 bg-plex-orange text-black font-semibold text-sm rounded-lg hover:bg-plex-orange/90 transition-colors disabled:opacity-50 flex items-center gap-2"
        >
          {saving && <Loader2 size={14} className="animate-spin" />}
          Save Settings
        </button>
        {saved && (
          <span className="flex items-center gap-1.5 text-sm text-green-400">
            <CheckCircle2 size={15} /> Saved
          </span>
        )}
        {saveError && (
          <p className="flex items-center gap-1.5 text-sm text-red-400">
            <XCircle size={15} /> {saveError}
          </p>
        )}
        <button
          onClick={restartScanner}
          disabled={restarting}
          className="px-4 py-2.5 bg-plex-card border border-plex-border text-sm rounded-lg text-gray-300 hover:border-plex-orange/50 hover:text-white transition-colors disabled:opacity-50 flex items-center gap-2"
          title="Restart the scanner pool — picks up config changes and reorders the queue"
        >
          <RotateCcw size={14} className={restarting ? 'animate-spin' : ''} />
          Restart Scanner
        </button>
        {restarted && (
          <span className="flex items-center gap-1.5 text-sm text-green-400">
            <CheckCircle2 size={15} /> Restarting — queue will reorder within ~10s
          </span>
        )}
      </div>
    </div>
  )
}

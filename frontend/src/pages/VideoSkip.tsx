import { useEffect, useState } from 'react'
import { api } from '../api/client'
import {
  Sparkles,
  RefreshCw,
  Plus,
  Pause,
  Play,
  RotateCcw,
  Trash2,
  Search,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Loader2,
} from 'lucide-react'

interface LibraryCoverage {
  library_id: string
  library_title: string
  total: number
  with_segments: number
  with_videoskip: number
  without_segments: number
  without_videoskip: number
}

interface Coverage {
  total: number
  with_segments: number
  with_videoskip: number
  without_segments: number
  without_videoskip: number
  libraries: LibraryCoverage[]
}

interface QueueRecent {
  title: string
  status: string
  skip_id: string
  imported: number
  last_error: string
  updated_at: string
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

interface SearchFile {
  skip_id: string
  author: string
  filters: string
  downloads: number
  recommended: boolean
}

interface SearchResult {
  video_id: string
  title: string
  display_title: string
  year: number | null
  imdb_id: string
  skip_files: SearchFile[]
}

const STATUS_STYLES: Record<string, string> = {
  done: 'text-green-400',
  imported: 'text-green-400',
  no_match: 'text-gray-500',
  failed: 'text-red-400',
  skipped: 'text-amber-400',
}

export default function VideoSkip() {
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [queue, setQueue] = useState<QueueStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [action, setAction] = useState('')
  const [message, setMessage] = useState<{ ok: boolean; message: string } | null>(null)
  const [query, setQuery] = useState('Oppenheimer')
  const [searching, setSearching] = useState(false)
  const [results, setResults] = useState<SearchResult[] | null>(null)
  const [searchError, setSearchError] = useState('')

  const refresh = async () => {
    setLoading(true)
    try {
      const [c, q] = await Promise.all([
        api.get<Coverage>('/api/videoskip/coverage'),
        api.get<QueueStatus>('/api/videoskip/queue'),
      ])
      setCoverage(c)
      setQueue(q)
    } catch {
      setMessage({ ok: false, message: 'Could not load VideoSkip status.' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // Queue depth changes while a sweep runs; polling keeps the page honest
    // without touching the Exchange (this is local DB state only).
    const id = setInterval(() => {
      api.get<QueueStatus>('/api/videoskip/queue').then(setQueue).catch(() => {})
    }, 8000)
    return () => clearInterval(id)
  }, [])

  const run = async (
    key: string,
    fn: () => Promise<{ ok: boolean; message: string }>,
  ) => {
    setAction(key)
    setMessage(null)
    try {
      setMessage(await fn())
      await refresh()
    } catch (e: any) {
      setMessage({ ok: false, message: e?.message ?? 'Action failed.' })
    } finally {
      setAction('')
    }
  }

  const queueMissing = () =>
    run('queue', async () => {
      const r = await api.post<{ queued: number; candidates: number }>(
        '/api/videoskip/queue/enqueue',
        { limit: 50 },
      )
      return {
        ok: r.queued > 0,
        message: `Queued ${r.queued} of ${r.candidates} candidate(s).`,
      }
    })

  const togglePause = () =>
    run('pause', async () => {
      const pause = !queue?.paused
      await api.post(`/api/videoskip/queue/${pause ? 'pause' : 'resume'}`)
      return { ok: true, message: pause ? 'Queue paused.' : 'Queue resumed.' }
    })

  const retryFailed = () =>
    run('retry', async () => {
      const r = await api.post<{ queued: number }>('/api/videoskip/queue/retry-failed')
      return { ok: true, message: `Requeued ${r.queued} failed item(s).` }
    })

  const clearQueue = () =>
    run('clear', async () => {
      const r = await api.post<{ deleted: number }>('/api/videoskip/queue/clear')
      return { ok: true, message: `Cleared ${r.deleted} finished item(s).` }
    })

  const testExchange = () =>
    run('test', async () => {
      const r = await api.get<{
        ok: boolean
        results?: number
        latency_ms: number
        error?: string
      }>('/api/videoskip/health')
      return r.ok
        ? {
            ok: true,
            message: `Exchange reachable — ${r.results ?? 0} result(s) in ${r.latency_ms} ms.`,
          }
        : { ok: false, message: r.error || 'Exchange unreachable.' }
    })

  const search = async () => {
    if (!query.trim()) return
    setSearching(true)
    setSearchError('')
    setResults(null)
    try {
      const r = await api.get<{ results: SearchResult[] }>(
        `/api/videoskip/search?q=${encodeURIComponent(query)}`,
      )
      setResults(r.results)
    } catch (e: any) {
      setSearchError(e?.message ?? 'Search failed.')
    } finally {
      setSearching(false)
    }
  }

  const cards = [
    { label: 'Titles', value: coverage?.total ?? 0, tone: 'text-white' },
    { label: 'Have segments', value: coverage?.with_segments ?? 0, tone: 'text-green-400' },
    { label: 'From VideoSkip', value: coverage?.with_videoskip ?? 0, tone: 'text-blue-400' },
    { label: 'No VideoSkip', value: coverage?.without_videoskip ?? 0, tone: 'text-amber-400' },
  ]

  return (
    <div className="space-y-7">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-[0.22em] text-plex-orange/80">
            VideoSkip
          </p>
          <h1 className="mt-1 flex items-center gap-2 text-3xl font-semibold tracking-tight text-white">
            <Sparkles size={26} className="text-plex-orange" />
            Exchange coverage
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Community-timed skip files, matched by IMDb or title + year / show + S+E.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={testExchange}
            disabled={!!action}
            className="flex items-center gap-2 rounded-lg border border-plex-border bg-plex-card px-3 py-2 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
          >
            {action === 'test' ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Sparkles size={14} />
            )}
            Test Exchange
          </button>
          <button
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-2 rounded-lg border border-plex-border bg-plex-card px-3 py-2 text-xs text-gray-300 transition-colors hover:border-gray-500 hover:text-white disabled:opacity-40"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {cards.map(card => (
          <div
            key={card.label}
            className="rounded-xl border border-plex-border bg-plex-card/80 px-4 py-3 shadow-glow"
          >
            <p className="text-[11px] uppercase tracking-wide text-gray-500">{card.label}</p>
            <p className={`mt-2 text-2xl font-semibold tabular-nums ${card.tone}`}>
              {loading ? '—' : card.value}
            </p>
          </div>
        ))}
      </div>

      {message && (
        <p
          className={`flex items-center gap-2 text-xs ${
            message.ok ? 'text-green-400' : 'text-red-400'
          }`}
        >
          {message.ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
          {message.message}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Queue */}
        <section className="rounded-xl border border-plex-border bg-plex-card/80 p-4 shadow-glow">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-gray-200">Auto-fetch queue</h2>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] ${
                queue?.paused
                  ? 'bg-amber-500/15 text-amber-400'
                  : queue?.auto_fetch
                    ? 'bg-green-500/15 text-green-400'
                    : 'bg-white/5 text-gray-500'
              }`}
            >
              {queue?.paused
                ? 'Paused'
                : queue?.auto_fetch
                  ? 'Automatic'
                  : 'Manual only'}
            </span>
            <span className="ml-auto text-[11px] text-gray-500">
              {queue?.daily_used ?? 0}/{queue?.daily_limit || '∞'} today
            </span>
          </div>

          <div className="mt-3 grid grid-cols-4 gap-2 text-center">
            {[
              ['Queued', queue?.queued ?? 0, 'text-gray-200'],
              ['Running', queue?.running ?? 0, 'text-blue-400'],
              ['Done', queue?.done ?? 0, 'text-green-400'],
              ['Failed', queue?.failed ?? 0, 'text-red-400'],
            ].map(([label, value, tone]) => (
              <div key={String(label)} className="rounded-lg bg-black/15 px-2 py-2">
                <p className={`text-lg font-semibold tabular-nums ${tone}`}>{value}</p>
                <p className="text-[10px] uppercase tracking-wide text-gray-600">{label}</p>
              </div>
            ))}
          </div>

          {queue?.current && (
            <p className="mt-3 truncate text-xs text-plex-orange">
              Now: {queue.current.title}
            </p>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={queueMissing}
              disabled={!!action || (coverage?.without_videoskip ?? 0) === 0}
              className="flex items-center gap-1.5 rounded-lg border border-plex-border bg-plex-card px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
            >
              {action === 'queue' ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Plus size={13} />
              )}
              Queue missing (50)
            </button>
            <button
              onClick={togglePause}
              disabled={!!action}
              className="flex items-center gap-1.5 rounded-lg border border-plex-border bg-plex-card px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
            >
              {queue?.paused ? <Play size={13} /> : <Pause size={13} />}
              {queue?.paused ? 'Resume' : 'Pause'}
            </button>
            <button
              onClick={retryFailed}
              disabled={!!action || (queue?.failed ?? 0) === 0}
              className="flex items-center gap-1.5 rounded-lg border border-plex-border bg-plex-card px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
            >
              <RotateCcw size={13} /> Retry failed
            </button>
            <button
              onClick={clearQueue}
              disabled={!!action || (queue?.total ?? 0) === 0}
              className="flex items-center gap-1.5 rounded-lg border border-plex-border bg-plex-card px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-red-400/50 hover:text-red-300 disabled:opacity-40"
            >
              <Trash2 size={13} /> Clear finished
            </button>
          </div>

          <div className="mt-4 border-t border-plex-border/60 pt-3">
            <p className="mb-2 text-[11px] uppercase tracking-wide text-gray-600">
              Recent activity
            </p>
            {queue?.recent?.length ? (
              <div className="space-y-1.5">
                {queue.recent.slice(0, 5).map((row, index) => (
                  <div
                    key={`${row.title}-${index}`}
                    className="flex items-center gap-2 rounded-lg bg-black/15 px-2 py-1.5 text-[11px]"
                  >
                    <span
                      className={`w-16 flex-shrink-0 font-medium ${
                        STATUS_STYLES[row.status] ?? 'text-gray-400'
                      }`}
                    >
                      {row.status}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-gray-300">{row.title}</span>
                    {row.skip_id && (
                      <span className="text-gray-600">#{row.skip_id}</span>
                    )}
                    {row.imported > 0 && (
                      <span className="text-green-500">+{row.imported}</span>
                    )}
                    {row.last_error && (
                      <span title={row.last_error} className="text-red-400">
                        <AlertTriangle size={12} />
                      </span>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-gray-600">Nothing processed yet.</p>
            )}
          </div>
        </section>

        {/* Coverage by library */}
        <section className="rounded-xl border border-plex-border bg-plex-card/80 p-4 shadow-glow">
          <h2 className="text-sm font-semibold text-gray-200">Coverage by library</h2>
          <p className="mt-1 text-xs text-gray-500">
            “Queue missing” targets titles with no VideoSkip data, not titles with
            no segments.
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-gray-600">
                <tr>
                  <th className="pb-2 text-left font-medium">Library</th>
                  <th className="pb-2 text-right font-medium">Titles</th>
                  <th className="pb-2 text-right font-medium">Segments</th>
                  <th className="pb-2 text-right font-medium">VideoSkip</th>
                  <th className="pb-2 text-right font-medium">No VS</th>
                </tr>
              </thead>
              <tbody>
                {coverage?.libraries.map(lib => (
                  <tr key={lib.library_id || lib.library_title} className="border-t border-plex-border/50">
                    <td className="py-2 pr-2 text-gray-200">{lib.library_title}</td>
                    <td className="px-1 py-2 text-right tabular-nums text-gray-400">{lib.total}</td>
                    <td className="px-1 py-2 text-right tabular-nums text-green-400">{lib.with_segments}</td>
                    <td className="px-1 py-2 text-right tabular-nums text-blue-400">{lib.with_videoskip}</td>
                    <td className="px-1 py-2 text-right tabular-nums text-amber-400">{lib.without_videoskip}</td>
                  </tr>
                ))}
                {!coverage?.libraries.length && (
                  <tr>
                    <td colSpan={5} className="py-4 text-center text-gray-600">
                      No libraries synced yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {/* Search */}
      <section className="rounded-xl border border-plex-border bg-plex-card/80 p-4 shadow-glow">
        <h2 className="text-sm font-semibold text-gray-200">Search the Exchange</h2>
        <p className="mt-1 text-xs text-gray-500">
          Discovery only. Apply a file from the title’s VideoSkip tab in the Library.
        </p>
        <div className="mt-3 flex gap-2">
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && search()}
            placeholder="Movie or show title…"
            className="min-w-0 flex-1 rounded-lg border border-plex-border bg-plex-bg px-3 py-2 text-sm text-gray-100 placeholder-gray-600 focus:border-plex-orange/60 focus:outline-none"
          />
          <button
            onClick={search}
            disabled={searching || !query.trim()}
            className="flex items-center gap-1.5 rounded-lg bg-plex-orange/15 px-3 py-2 text-xs text-plex-orange ring-1 ring-inset ring-plex-orange/30 transition-colors hover:bg-plex-orange/25 disabled:opacity-40"
          >
            {searching ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            Search
          </button>
        </div>

        {searchError && <p className="mt-3 text-xs text-red-400">{searchError}</p>}

        {results && (
          <div className="mt-3 space-y-2">
            {results.length === 0 && (
              <p className="text-xs text-gray-600">No results.</p>
            )}
            {results.slice(0, 8).map(result => (
              <div
                key={result.video_id}
                className="rounded-lg border border-plex-border/70 bg-black/15 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-gray-200">
                    {result.display_title}
                  </span>
                  {result.year && <span className="text-[11px] text-gray-500">{result.year}</span>}
                  {result.imdb_id && (
                    <span className="font-mono text-[10px] text-gray-600">{result.imdb_id}</span>
                  )}
                  <span className="ml-auto text-[11px] text-gray-500">
                    {result.skip_files.length} file(s)
                  </span>
                </div>
                {result.skip_files.slice(0, 3).map(file => (
                  <p key={file.skip_id} className="mt-1 text-[11px] text-gray-500">
                    <span className="text-gray-400">{file.filters || 'No description'}</span>
                    {' '}· {file.downloads} downloads
                    {file.recommended && <span className="ml-1 text-plex-orange">recommended</span>}
                  </p>
                ))}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

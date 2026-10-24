import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Upload, ClipboardPaste, Download, AlertTriangle, Check, Search, RefreshCw } from 'lucide-react'

interface ImportResult {
  imported: number
  source: string
  title: string
  warning: string | null
  categories?: Record<string, number>
}

interface VideoSkipFile {
  skip_id: string
  author: string
  uploaded: string
  filters: string
  downloads: number
  recommended: boolean
}

interface VideoSkipMatch {
  video_id: string
  title: string
  display_title: string
  year: number | null
  imdb_id: string
  skip_files: VideoSkipFile[]
  recommended_skip_id: string
}

interface VideoSkipResponse {
  query: string
  existing_segments: number
  matches: VideoSkipMatch[]
}

interface Props {
  plexGuid: string
  onImported?: (count: number) => void
}

type Mode = 'file' | 'paste' | 'videoskip'

export default function SkipFileImport({ plexGuid, onImported }: Props) {
  const [mode, setMode] = useState<Mode>('file')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ImportResult | null>(null)
  const [error, setError] = useState('')
  const [pasted, setPasted] = useState('')
  const [replace, setReplace] = useState(false)
  const [vsLoading, setVsLoading] = useState(false)
  const [vsError, setVsError] = useState('')
  const [vsData, setVsData] = useState<VideoSkipResponse | null>(null)
  const [vsReload, setVsReload] = useState(0)
  const [applyingSkip, setApplyingSkip] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const finish = (res: ImportResult) => {
    setResult(res)
    onImported?.(res.imported)
  }

  // Load candidates when the VideoSkip tab opens, when the title changes, or when
  // the user presses refresh. `cancelled` prevents a stale response overwriting a
  // newer one if the panel is closed mid-request.
  useEffect(() => {
    if (mode !== 'videoskip') return
    let cancelled = false
    setVsLoading(true)
    setVsError('')
    api.get<VideoSkipResponse>(`/api/videoskip/title/${encodeURIComponent(plexGuid)}`)
      .then(data => { if (!cancelled) setVsData(data) })
      .catch(e => { if (!cancelled) setVsError(e instanceof Error ? e.message : 'Lookup failed.') })
      .finally(() => { if (!cancelled) setVsLoading(false) })
    return () => { cancelled = true }
  }, [mode, plexGuid, vsReload])

  const applyVideoSkip = async (file: VideoSkipFile) => {
    setApplyingSkip(file.skip_id)
    setError('')
    setResult(null)
    try {
      const res = await api.post<ImportResult>('/api/videoskip/apply', {
        plex_guid: plexGuid,
        skip_id: file.skip_id,
        replace,
      })
      finish(res)
      setVsData(prev => (prev ? { ...prev, existing_segments: res.imported } : prev))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.')
    } finally {
      setApplyingSkip(null)
    }
  }

  const importFile = async (file: File) => {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const form = new FormData()
      form.append('plex_guid', plexGuid)
      form.append('file', file)
      form.append('replace', String(replace))
      finish(await api.upload<ImportResult>('/api/segments/import', form))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.')
    } finally {
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  const importPaste = async () => {
    setBusy(true)
    setError('')
    setResult(null)
    try {
      finish(
        await api.post<ImportResult>('/api/segments/import/paste', {
          plex_guid: plexGuid,
          text: pasted,
          replace,
        }),
      )
      setPasted('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed.')
    } finally {
      setBusy(false)
    }
  }

  const exportAs = async (fmt: 'edl' | 'mcf') => {
    const res = await api.get<{ body: string }>(
      `/api/segments/export/${encodeURIComponent(plexGuid)}?fmt=${fmt}`,
    )
    // Shown rather than downloaded: the viewer sandbox blocks page-initiated
    // downloads, so copying from a textarea is the reliable path.
    setPasted(res.body)
    setMode('paste')
  }

  return (
    <div className="bg-plex-card border border-plex-border rounded-xl p-4 space-y-3">
      <div className="flex items-center gap-2">
        <button
          onClick={() => setMode('file')}
          className={`px-2.5 py-1 rounded text-xs flex items-center gap-1 border ${
            mode === 'file'
              ? 'bg-plex-orange border-plex-orange text-white'
              : 'border-plex-border text-gray-400'
          }`}
        >
          <Upload size={12} /> Skip file
        </button>
        <button
          onClick={() => setMode('paste')}
          className={`px-2.5 py-1 rounded text-xs flex items-center gap-1 border ${
            mode === 'paste'
              ? 'bg-plex-orange border-plex-orange text-white'
              : 'border-plex-border text-gray-400'
          }`}
        >
          <ClipboardPaste size={12} /> Paste list
        </button>
        <button
          onClick={() => setMode('videoskip')}
          className={`px-2.5 py-1 rounded text-xs flex items-center gap-1 border ${
            mode === 'videoskip'
              ? 'bg-plex-orange border-plex-orange text-white'
              : 'border-plex-border text-gray-400'
          }`}
        >
          <Search size={12} /> VideoSkip
        </button>

        <label className="ml-auto flex items-center gap-1.5 text-xs text-gray-500">
          <input
            type="checkbox"
            checked={replace}
            onChange={e => setReplace(e.target.checked)}
            className="accent-plex-orange"
          />
          Replace existing
        </label>
      </div>

      {mode === 'file' ? (
        <div>
          <input
            ref={fileInput}
            type="file"
            accept=".skp,.edl,.mcf,.txt"
            disabled={busy}
            onChange={e => {
              const file = e.target.files?.[0]
              if (file) importFile(file)
            }}
            className="block w-full text-xs text-gray-400 file:mr-3 file:py-1.5 file:px-3 file:rounded file:border-0 file:text-xs file:bg-plex-border file:text-gray-200 hover:file:bg-plex-border/70"
          />
          <p className="text-[11px] text-gray-600 mt-2">
            VideoSkip <code>.skp</code>, Kodi <code>.edl</code>, MovieContentFilter{' '}
            <code>.mcf</code>, or a plain <code>.txt</code> list. Importing skips the scan entirely.
          </p>
        </div>
      ) : mode === 'paste' ? (
        <div>
          <textarea
            value={pasted}
            onChange={e => setPasted(e.target.value)}
            rows={5}
            placeholder={'00:12:30 - 00:13:05 nudity\n1:02:11 to 1:02:20 violence'}
            className="w-full bg-plex-bg border border-plex-border rounded p-2 text-xs text-gray-200 font-mono"
          />
          <button
            onClick={importPaste}
            disabled={busy || !pasted.trim()}
            className="mt-2 px-3 py-1.5 rounded text-xs bg-plex-orange text-white disabled:opacity-40"
          >
            {busy ? 'Importing...' : 'Import list'}
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-start gap-2">
            <p className="text-[11px] text-gray-600 flex-1">
              Community-timed files from the VideoSkip Exchange, fetched on demand. Nothing is
              bundled with Cleanplex; the file is applied straight to this title.
            </p>
            <button
              onClick={() => setVsReload(n => n + 1)}
              disabled={vsLoading}
              title="Search the Exchange again"
              className="p-1 text-gray-500 hover:text-gray-200 disabled:opacity-40"
            >
              <RefreshCw size={12} className={vsLoading ? 'animate-spin' : ''} />
            </button>
          </div>

          {vsLoading || (!vsData && !vsError) ? (
            <p className="text-xs text-gray-500">Searching VideoSkip…</p>
          ) : vsError ? (
            <p className="text-xs text-red-400 flex items-start gap-1.5">
              <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {vsError}
            </p>
          ) : !vsData || vsData.matches.length === 0 ? (
            <p className="text-xs text-gray-600">
              No Exchange match for “{vsData?.query || 'this title'}”. Try uploading a file or pasting
              a timestamp list instead.
            </p>
          ) : (
            <div className="space-y-2">
              {vsData.matches.map((match, index) => (
                <div key={match.video_id} className="border border-plex-border rounded-lg p-2 space-y-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-gray-200">{match.display_title}</span>
                    {match.year && <span className="text-[11px] text-gray-500">{match.year}</span>}
                    {match.imdb_id && (
                      <span className="text-[11px] text-gray-600 font-mono">{match.imdb_id}</span>
                    )}
                    {index === 0 && (
                      <span className="text-[10px] bg-green-500/15 text-green-400 px-1.5 py-0.5 rounded">
                        best match
                      </span>
                    )}
                  </div>

                  <div className="space-y-1">
                    {[...match.skip_files]
                      .sort((a, b) => Number(b.recommended) - Number(a.recommended) || b.downloads - a.downloads)
                      .map(file => (
                        <div
                          key={file.skip_id}
                          className="flex items-center gap-2 bg-black/10 border border-plex-border/60 rounded px-2 py-1.5"
                        >
                          <div className="min-w-0 flex-1">
                            <p className="text-[11px] text-gray-300 truncate">
                              {file.filters || 'No filter description'}
                              {file.recommended && (
                                <span className="ml-1.5 text-[10px] text-plex-orange">recommended</span>
                              )}
                            </p>
                            <p className="text-[10px] text-gray-600">
                              by {file.author || 'unknown'} · {file.downloads} downloads
                              {file.uploaded ? ` · ${file.uploaded}` : ''}
                            </p>
                          </div>
                          <button
                            onClick={() => applyVideoSkip(file)}
                            disabled={applyingSkip !== null}
                            className="px-2 py-1 rounded text-[11px] bg-plex-orange/20 border border-plex-orange/30 text-plex-orange hover:bg-plex-orange/30 disabled:opacity-40 flex-shrink-0"
                          >
                            {applyingSkip === file.skip_id ? 'Applying…' : 'Apply'}
                          </button>
                        </div>
                      ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {vsData && vsData.existing_segments > 0 && !replace && (
            <p className="text-[11px] text-amber-400">
              This title already has {vsData.existing_segments} segment
              {vsData.existing_segments === 1 ? '' : 's'}. Tick “Replace existing” to overwrite them.
            </p>
          )}
        </div>
      )}

      <div className="flex items-center gap-2 pt-1 border-t border-plex-border">
        <span className="text-[11px] text-gray-600">Export:</span>
        <button onClick={() => exportAs('edl')} className="text-[11px] text-gray-400 hover:text-gray-200 flex items-center gap-1">
          <Download size={11} /> EDL
        </button>
        <button onClick={() => exportAs('mcf')} className="text-[11px] text-gray-400 hover:text-gray-200 flex items-center gap-1">
          <Download size={11} /> MCF
        </button>
      </div>

      {error && (
        <p className="text-xs text-red-400 flex items-start gap-1.5">
          <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {error}
        </p>
      )}

      {result && (
        <div className="text-xs space-y-1">
          <p className="text-green-400 flex items-center gap-1.5">
            <Check size={12} /> Imported {result.imported} segment
            {result.imported === 1 ? '' : 's'} from {result.source}.
          </p>
          {result.categories && Object.keys(result.categories).length > 0 && (
            <p className="text-gray-500">
              Categories:{' '}
              {Object.entries(result.categories)
                .map(([category, count]) => `${category} ${count}`)
                .join(', ')}
            </p>
          )}
          {result.warning && (
            <p className="text-amber-400 flex items-start gap-1.5">
              <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" /> {result.warning}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

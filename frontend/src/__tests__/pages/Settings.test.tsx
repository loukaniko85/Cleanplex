import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Settings from '../../pages/Settings'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

const defaultSettings = {
  plex_url: 'http://plex:32400',
  plex_token: 'abc',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED"]',
  nudenet_model: '320n',
  nudenet_model_path: '',
  videoskip_auto_fetch: '0',
  videoskip_auto_daily_limit: '50',
  videoskip_auto_delay_ms: '1500',
  videoskip_auto_sweep_minutes: '60',
  videoskip_auto_max_attempts: '2',
  videoskip_auto_libraries: '[]',
  videoskip_auto_respect_scan_window: '0',
}

const coverage = {
  total: 10,
  with_segments: 6,
  with_videoskip: 4,
  without_segments: 4,
  without_videoskip: 6,
  libraries: [
    {
      library_id: '1',
      library_title: 'Movies',
      total: 10,
      with_segments: 6,
      with_videoskip: 4,
      without_segments: 4,
      without_videoskip: 6,
    },
  ],
}

const scanCoverage = {
  total: 10,
  scanned: 2,
  pending: 5,
  scanning: 0,
  failed: 3,
  with_segments: 2,
  with_nudenet: 2,
  without_segments: 8,
  without_nudenet: 8,
  last_error: {
    title: 'Treasure Island – S1E3',
    error: 'File not found: /tv/Treasure Island/x.mkv',
    at: '2026-10-25 17:00:00',
  },
  libraries: [
    {
      library_id: '1',
      library_title: 'Movies',
      total: 10,
      scanned: 2,
      pending: 5,
      failed: 3,
      with_segments: 2,
      with_nudenet: 2,
      without_segments: 8,
      without_nudenet: 8,
    },
  ],
}

const webhookStatus = {
  total: 1,
  last_received_at: '2026-10-24 06:00:00',
  auto_fetch: false,
  token_required: false,
  events: [
    {
      received_at: '2026-10-24 06:00:00',
      event: 'library.new',
      rating_key: '10',
      title: 'Oppenheimer',
      result: 'queued',
      detail: '',
    },
  ],
}

const queueStatus = {
  auto_fetch: false,
  paused: false,
  queued: 2,
  running: 0,
  done: 8,
  no_match: 1,
  failed: 0,
  skipped: 0,
  total: 11,
  daily_used: 9,
  daily_limit: 50,
  current: null,
  recent: [],
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>
  )
}

beforeEach(() => {
  // shouldAdvanceTime lets the real clock keep moving under the fake timers, so
  // testing-library's waitFor can still poll. Without it every waitFor in this
  // file hangs until the 5s test timeout.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockApi.get.mockImplementation((path: string) => {
    // detector-labels is checked first: it also contains 'settings' in its path.
    if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
    if (path.includes('videoskip/coverage')) return Promise.resolve(coverage)
    if (path.includes('scan/coverage')) return Promise.resolve(scanCoverage)
    if (path.includes('videoskip/queue')) return Promise.resolve(queueStatus)
    if (path.includes('webhooks/plex/status')) return Promise.resolve(webhookStatus)
    // /api/settings returns the settings object flat, not wrapped.
    if (path.includes('settings')) return Promise.resolve(defaultSettings)
    if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Settings', () => {
  it('renders the page heading', async () => {
    renderSettings()
    await waitFor(() => expect(screen.getByText('Settings')).toBeInTheDocument())
  })

  it('loads and displays plex url from settings', async () => {
    renderSettings()
    await waitFor(() => {
      const input = screen.getByDisplayValue('http://plex:32400')
      expect(input).toBeInTheDocument()
    })
  })

  it('shows VideoSkip coverage and queues missing titles', async () => {
    mockApi.post.mockResolvedValue({ queued: 2, candidates: 6 })

    renderSettings()
    await waitFor(() => expect(screen.getByText('VideoSkip Exchange')).toBeInTheDocument())
    expect(screen.getByText('From VideoSkip')).toBeInTheDocument()
    // Two coverage tables now render "Movies" (NudeNet + VideoSkip).
    expect(screen.getAllByText('Movies').length).toBeGreaterThan(0)
    expect(screen.getByText('9/50 today')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Queue missing \(50\)/ }))

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith('/api/videoskip/queue/enqueue', { limit: 50 }),
    )
    expect(await screen.findByText(/Queued 2 of 6/)).toBeInTheDocument()
  })

  it('surfaces failed NudeNet scans and retries them', async () => {
    mockApi.post.mockResolvedValue({ requeued: 3 })

    renderSettings()
    await waitFor(() => expect(screen.getByText(/3 failed/)).toBeInTheDocument())
    expect(screen.getByText(/File not found/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Retry failed scans/ }))

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith('/api/scan/retry-failed'),
    )
    expect(await screen.findByText(/Requeued 3 failed scan/)).toBeInTheDocument()
  })
})

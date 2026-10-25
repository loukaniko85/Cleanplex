import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Analytics from '../../pages/Analytics'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

function coverage(overrides: Record<string, unknown> = {}) {
  return {
    total: 10,
    scanned: 4,
    pending: 2,
    failed: 0,
    with_nudenet: 2,
    last_error: null,
    ...overrides,
  }
}

function mockGet({ nudenet }: { nudenet: unknown }) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('analytics/nudenet')) return Promise.resolve(nudenet)
    if (path.includes('analytics/labels')) {
      return Promise.resolve({ labels: [{ label: 'violence skip 3', count: 12 }] })
    }
    if (path.includes('analytics/skips/')) return Promise.reject(new Error('not relevant'))
    return Promise.resolve({})
  })
}

function renderAnalytics() {
  return render(
    <MemoryRouter>
      <Analytics />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Analytics — NudeNet', () => {
  it('shows detector labels with confidence and coverage counts', async () => {
    mockGet({
      nudenet: {
        segments: 2,
        labels: [
          {
            label: 'FEMALE_BREAST_EXPOSED',
            count: 2,
            avg_confidence: 0.82,
            duration_ms: 6000,
          },
        ],
        coverage: coverage({ scanned: 4, with_nudenet: 2 }),
      },
    })

    renderAnalytics()

    await waitFor(() => expect(screen.getByText('NudeNet Detections')).toBeInTheDocument())
    expect(screen.getByText(/F Breast · 82%/)).toBeInTheDocument()
    expect(screen.getByText('With detections')).toBeInTheDocument()
  })

  it('explains File not found failures instead of showing optimistic zeros', async () => {
    mockGet({
      nudenet: {
        segments: 0,
        labels: [],
        coverage: coverage({
          scanned: 0,
          with_nudenet: 0,
          failed: 24352,
          pending: 0,
          last_error: {
            title: 'Treasure Island – S1E3',
            error: 'File not found: /tv/Treasure Island/x.mkv',
            at: '2026-10-25 17:00:00',
          },
        }),
      },
    })

    renderAnalytics()

    await waitFor(() =>
      expect(screen.getByText(/24,352 scan\(s\) failed/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/Cleanplex cannot see these files/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /retry failed scans in Settings/i })).toBeInTheDocument()
  })

  it('shows a queued empty state when nothing has been scanned yet', async () => {
    mockGet({
      nudenet: {
        segments: 0,
        labels: [],
        coverage: coverage({ scanned: 0, with_nudenet: 0, failed: 0, pending: 105 }),
      },
    })

    renderAnalytics()

    await waitFor(() =>
      expect(screen.getByText(/No scans have completed yet/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/105 title\(s\) queued/)).toBeInTheDocument()
  })
})

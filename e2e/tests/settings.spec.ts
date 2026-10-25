import { test, expect } from '@playwright/test'

// The retry test mutates the seeded database (failed → pending), so it runs last
// in this file. analytics.spec.ts runs before this file and relies on the
// original failed rows.
test.describe('Settings', () => {
  test('VideoSkip coverage still renders alongside NudeNet', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.getByRole('heading', { name: 'VideoSkip Exchange' })).toBeVisible()
    await expect(page.getByText('From VideoSkip', { exact: true })).toBeVisible()
  })

  test('shows local scan coverage with the failure cause and retries failed scans', async ({ page }) => {
    await page.goto('/settings')

    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    const section = page.locator('section', { hasText: 'Local scanning (NudeNet)' })
    await expect(section).toBeVisible()
    await expect(section.getByText('Scanned', { exact: true }).first()).toBeVisible()

    // Failures are visible instead of a wall of zeros.
    await expect(section.getByText('2 failed')).toBeVisible()
    await expect(section.getByText(/File not found/)).toBeVisible()

    await section.getByRole('button', { name: /Retry failed scans/ }).click()
    await expect(page.getByText(/Requeued 2 failed scan/)).toBeVisible()
  })
})

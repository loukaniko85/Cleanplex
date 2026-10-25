import { test, expect } from '@playwright/test'

// Runs before settings.spec.ts (alphabetical), which mutates the seeded data by
// retrying failed scans. Read-only assertions only in this file.
test.describe('Analytics', () => {
  test('shows NudeNet detections with confidence and drill-down', async ({ page }) => {
    await page.goto('/analytics')

    await expect(page.getByRole('heading', { name: 'Analytics', exact: true })).toBeVisible()
    await expect(page.getByText('NudeNet Detections')).toBeVisible()

    // Two scanner titles seeded; the bar shows the NudeNet class + confidence.
    await expect(page.getByText(/F Breast · 90%/)).toBeVisible()

    await page.getByText(/F Breast · 90%/).click()
    await expect(page.getByText(/by content rating/i)).toBeVisible()
  })

  test('explains failed scans instead of hiding them', async ({ page }) => {
    await page.goto('/analytics')

    await expect(page.getByText(/2 scan\(s\) failed/)).toBeVisible()
    await expect(page.getByText(/Cleanplex cannot see these files/)).toBeVisible()
    await expect(
      page.getByRole('link', { name: /retry failed scans in Settings/i }),
    ).toBeVisible()
  })
})

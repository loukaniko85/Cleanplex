import { test, expect } from '@playwright/test'

const LION_KING_LIST = [
  '04:38 - 07:15 Scar Missed Presentation of Simba',
  '25:31 - 40:56 Scar and Hyenas meeting + Stampede',
].join('\n')

test.describe('Library — paste import', () => {
  test('offers the WhenJumpScare tab', async ({ page }) => {
    await page.goto('/library')
    await page.getByRole('button', { name: 'Movies', exact: true }).click()
    await expect(page.getByText('Movie One')).toBeVisible()
    await page
      .locator('button[title="Import a skip file or fetch this title from VideoSkip"]')
      .first()
      .click()
    await page.getByRole('button', { name: 'Jump scares' }).click()

    await expect(page.getByText(/Human-timed jumpscares from WhenJumpScare/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Fetch jump scares' })).toBeVisible()
  })

  test('pastes a scene list and applies a category override through the UI', async ({ page }) => {
    await page.goto('/library')

    // The seeded DB supplies libraries even though Plex is unreachable.
    await page.getByRole('button', { name: 'Movies', exact: true }).click()
    await expect(page.getByText('Movie One')).toBeVisible()

    await page
      .locator('button[title="Import a skip file or fetch this title from VideoSkip"]')
      .first()
      .click()

    await page.getByRole('button', { name: 'Paste list' }).click()
    await page.locator('textarea').fill(LION_KING_LIST)
    await page
      .getByLabel('Category for lines without a keyword')
      .selectOption('violence')
    await page.getByRole('button', { name: 'Import list' }).click()

    await expect(page.getByText('Imported 2 segments from paste.')).toBeVisible()
    // Regression: the panel used to collapse on import, hiding this result.
    await expect(page.getByText('F Breast').first()).toBeVisible()

    // The pasted cues are stored as violence skips (verified through the API;
    // the segment list only renders detector labels).
    const res = await page.request.get('/api/titles/m1/segments')
    const body = await res.json()
    const pasted = body.segments.filter((s: { source: string }) => s.source === 'paste')
    expect(pasted).toHaveLength(2)
    expect(pasted.every((s: { category: string }) => s.category === 'violence')).toBe(true)
  })
})

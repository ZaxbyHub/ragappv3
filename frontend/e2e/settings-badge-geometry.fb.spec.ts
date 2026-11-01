import { expect, test } from "@playwright/test";

// Supplemental real-browser geometry coverage for #779. This belongs in the
// existing smoke project so it uses the same stub backend and production
// preview contract as the issue #573 smoke suite. Keep the frozen #778 a11y
// matrix file unchanged.
test.describe("settings status badges at narrow width (#779)", () => {
  test("wrap without clipping any actual badge at either edge", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto("/login");
    await page.fill("#login-username", "e2e-user");
    await page.fill("#login-password", "e2e-pass");
    await page.getByRole("button", { name: /sign in/i }).click();
    await page.waitForURL(/\/documents(?:$|[?#])/);
    await page.goto("/settings");

    const badgeLabels = ["Backend", "Embeddings", "Chat"] as const;
    const badgeLocator = (label: string) =>
      page.locator("span.inline-flex").filter({ hasText: new RegExp(`^\\s*${label}\\s*$`) });
    const row = badgeLocator("Backend").locator("..");
    await expect(row).toBeVisible();
    const bounds = await row.boundingBox();
    expect(bounds).not.toBeNull();
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport!.width);

    const badgeYPositions = new Set<number>();
    for (const label of badgeLabels) {
      const badge = badgeLocator(label);
      await expect(badge).toHaveCount(1);
      await expect(badge).toBeVisible();
      const badgeBounds = await badge.boundingBox();
      expect(badgeBounds).not.toBeNull();
      expect(badgeBounds!.x).toBeGreaterThanOrEqual(0);
      expect(badgeBounds!.x + badgeBounds!.width).toBeLessThanOrEqual(viewport!.width);
      badgeYPositions.add(Math.round(badgeBounds!.y));
    }

    expect(badgeYPositions.size).toBeGreaterThan(1);
  });
});

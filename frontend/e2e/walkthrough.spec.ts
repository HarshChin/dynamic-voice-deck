import { expect, test } from "@playwright/test";

import { ask, log, slideNumber, startSession } from "./app";
import { startBackend, stopBackend } from "./backend";

/**
 * The release acceptance scenario (PRD §13, TC-E2E-001).
 *
 * Questions are typed rather than spoken. The transcriber is a fake and returns the same sentence
 * for any audio, so speaking cannot express *which* question is being asked; what the voice path
 * contributes to this scenario is the interruption, and that is driven for real, by holding the
 * push-to-talk key while the agent is speaking.
 */

test.beforeAll(startBackend);
test.afterAll(stopBackend);

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("backend: ok")).toBeVisible();
});

test("TC-E2E-001: presents the deck, is interrupted, and answers what it is asked", async ({
  page,
}) => {
  // 1. Start a session (PRD §13 step 1, F2).
  await startSession(page);

  // 2. The walkthrough presents the deck on its own (step 2, F8).
  await page.getByRole("button", { name: "Walk me through it" }).click();
  await expect.poll(() => slideNumber(page), { timeout: 20_000 }).toBeGreaterThan(1);

  // 3. Interrupt it by talking over it (step 3, F7). Push-to-talk is used so the moment of onset
  //    is the test's decision rather than the fake microphone's.
  await page.getByRole("button", { name: "Push to talk" }).click();
  await page.keyboard.down(" ");
  await expect(page.getByText("listening — release to send")).toBeVisible();
  await page.keyboard.up(" ");
  await expect(
    log(page)
      .getByText(/interrupted/i)
      .first(),
  ).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "Push to talk" }).click();

  // 4. Ask about interruption; the deck follows the question (step 4, F5).
  await ask(page, "How do you handle interruptions?");
  await expect.poll(() => slideNumber(page), { timeout: 20_000 }).toBe(4);
  await expect(log(page).getByText("Two layers, actually.")).toBeVisible();

  // 8. The log carries the tool call that moved the deck, with its reason (step 8, F10).
  await expect(
    log(page)
      .getByText(/go_to_slide/)
      .first(),
  ).toBeVisible();
  await expect(
    log(page)
      .getByText(/asked about interruption/)
      .first(),
  ).toBeVisible();
});

test("TC-E2E-001: resumes the tour from where it was cut off, not from the beginning", async ({
  page,
}) => {
  await startSession(page);
  await page.getByRole("button", { name: "Walk me through it" }).click();
  await expect.poll(() => slideNumber(page), { timeout: 20_000 }).toBeGreaterThan(1);

  // Cut it off with a question. Where it stopped is read afterwards rather than before: synthesis
  // is faked here and therefore instant, so the tour keeps moving while the question is typed.
  await ask(page, "Wait, hold on.");
  await expect(log(page).getByText(/pieces fit together/)).toBeVisible({ timeout: 20_000 });
  const cutAt = await slideNumber(page);
  expect(cutAt).toBeGreaterThan(1);

  // "Carry on" continues the tour rather than starting it again (step 6, F8).
  await ask(page, "Carry on.");
  await expect.poll(() => slideNumber(page), { timeout: 20_000 }).toBeGreaterThanOrEqual(cutAt);
  expect(await slideNumber(page)).not.toBe(1);
});

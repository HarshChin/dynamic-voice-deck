import { expect, test } from "@playwright/test";

import { ask, log, showNotices, slideNumber, startSession } from "./app";
import { startBackend, stopBackend } from "./backend";

/**
 * Manual navigation is told to the agent (TC-E2E-004, F9).
 *
 * The deck is a shared surface: if the user moves it and the agent does not know, the next answer
 * is about the wrong slide, which is the most obvious way this product can look broken.
 */

test.beforeAll(startBackend);
test.afterAll(stopBackend);

test("TC-E2E-004: moving the deck by hand is logged, and the agent answers without moving it", async ({
  page,
}) => {
  await page.goto("/");
  await startSession(page);
  // Manual navigation is reported as a client-side notice, which the log hides by default: it is
  // detail during debugging, noise during a demo (PRD F10).
  await showNotices(page);

  await page.keyboard.press("ArrowRight");
  await expect(log(page).getByText(/moved to slide 2 by hand/)).toBeVisible();
  expect(await slideNumber(page)).toBe(2);

  await ask(page, "Explain this one.");
  await expect(log(page).getByText(/how the pieces fit together/)).toBeVisible({
    timeout: 20_000,
  });

  // The answer arrived and the deck stayed where the user put it.
  expect(await slideNumber(page)).toBe(2);
  await expect(log(page).getByText(/go_to_slide/)).toHaveCount(0);
});

import { expect, test } from "@playwright/test";

import { ask, log, slideNumber, startSession } from "./app";
import { startBackend, stopBackend } from "./backend";

/**
 * The text path with no microphone (TC-E2E-003, F13).
 *
 * A voice-first app that is useless without a microphone is a demo that fails on a locked-down
 * laptop, in a noisy room, or on a call. Typing has to reach exactly the same agent.
 */

test.use({ permissions: [] });

test.beforeAll(startBackend);
test.afterAll(stopBackend);

test("TC-E2E-003: with the microphone refused, typing still moves the deck and gets an answer", async ({
  page,
  context,
}) => {
  await context.clearPermissions();
  await page.goto("/");
  await startSession(page);

  await ask(page, "Tell me about the latency budget.");

  await expect(log(page).getByText(/About a second and a half/)).toBeVisible({ timeout: 20_000 });
  await expect.poll(() => slideNumber(page)).toBe(2);
});

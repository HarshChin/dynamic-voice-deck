import { expect, test } from "@playwright/test";

import { log, startSession } from "./app";
import { startBackend, stopBackend } from "./backend";

/**
 * What the app does when the backend is not there (TC-E2E-002, F2).
 *
 * The failure has to be legible. A voice app that silently fails to connect looks identical to one
 * that is listening, which is the worst outcome: the user talks to nothing.
 */

test.afterAll(stopBackend);

test("TC-E2E-002: says the backend is unreachable, and works once it is up", async ({ page }) => {
  await stopBackend();

  await page.goto("/");
  await expect(page.getByText("backend: unreachable")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Nothing loaded yet")).toBeVisible();

  await page.getByRole("button", { name: "Start session" }).click();
  // One automatic retry, then it says so rather than retrying for ever (TR-175).
  await expect(log(page).getByText(/connection lost/i)).toBeVisible({ timeout: 25_000 });

  await startBackend();
  await page.reload();
  await expect(page.getByText("backend: ok")).toBeVisible({ timeout: 20_000 });

  await startSession(page);
  await expect(page.getByText("Anatomy of a Voice Agent").first()).toBeVisible();
});

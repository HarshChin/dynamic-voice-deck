import { expect, type Locator, type Page } from "@playwright/test";

/**
 * The handful of things every end-to-end spec needs to say.
 *
 * Kept in one place because these are the selectors most likely to drift: a heading renamed or a
 * label reworded should break one file, not four.
 */

/** The event log panel, which is open by default. */
export function log(page: Page): Locator {
  return page.getByLabel("Event log");
}

/** The typed-question field (PRD F13). */
export function composer(page: Page): Locator {
  return page.getByLabel("Ask the presenter a question");
}

/**
 * The 1-based slide on screen, read from the slide's own eyebrow.
 *
 * @param page - The page under test.
 * @returns The slide number, or 0 when no slide is rendered.
 */
export async function slideNumber(page: Page): Promise<number> {
  const eyebrow = page.locator("article p").first();
  if ((await eyebrow.count()) === 0) {
    return 0;
  }
  const text = (await eyebrow.textContent()) ?? "";
  return Number(/\d+/.exec(text)?.[0] ?? 0);
}

/**
 * Open a session and wait until the agent is actually listening.
 *
 * Waiting for the button to flip to "End session" is not enough: it flips as soon as the socket
 * starts connecting, and anything sent in that window reaches no server at all. The orb saying
 * "Listening" is the first moment the session is real.
 *
 * @param page - The page under test.
 */
export async function startSession(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Start session" }).click();
  await expect(page.getByRole("button", { name: "End session" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Listening" })).toBeVisible({
    timeout: 20_000,
  });
}

/**
 * Turn on the debug toggle, which is what shows client-side notices in the log.
 *
 * @param page - The page under test.
 */
export async function showNotices(page: Page): Promise<void> {
  await page.getByLabel("debug").check();
}

/**
 * Type a question and send it.
 *
 * @param page - The page under test.
 * @param question - What to ask.
 */
export async function ask(page: Page, question: string): Promise<void> {
  await composer(page).fill(question);
  await page.getByRole("button", { name: "Ask" }).click();
}

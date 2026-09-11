import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

/**
 * Two seconds of silence, looped by Chromium as the microphone's input.
 *
 * Chromium's built-in fake device emits a continuous beep, which the energy detector correctly
 * hears as someone talking: the agent interrupts itself the moment it opens its mouth. Silence is
 * the honest stand-in for a quiet room, and the one test that needs speech drives it by key.
 */
const SILENT_MICROPHONE = path.resolve(
  fileURLToPath(new URL(".", import.meta.url)),
  "..",
  "backend",
  "tests",
  "fixtures",
  "audio",
  "silence_2s.wav",
);

/**
 * End-to-end configuration (TR-210, TC-E2E-001 to TC-E2E-004).
 *
 * The suite drives the shipped app in a real browser against a real server. Only the three
 * providers are replaced, by `STT_PROVIDER=fake LLM_PROVIDER=fake TTS_PROVIDER=fake`, because a
 * language model cannot promise the exact slide and the exact words an assertion needs, and the
 * point of these tests is the wiring between browser and server rather than the model's judgement.
 *
 * Vite serves the app so the tests run against the same dev setup a contributor has, with its
 * proxy pointing at the backend. The backend is started by the tests themselves rather than by
 * `webServer`, because one of them needs it to be down.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    video: "off",
    permissions: ["microphone"],
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: {
          args: [
            // A microphone that always exists, so the capture graph is real: the browser opens a
            // device, the worklet runs, and frames flow. What it plays is silence, see above.
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            `--use-file-for-fake-audio-capture=${SILENT_MICROPHONE}`,
            "--autoplay-policy=no-user-gesture-required",
          ],
        },
      },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});

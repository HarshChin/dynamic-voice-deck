import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// React Testing Library does not auto-clean when `globals` are provided by Vitest rather than by a
// framework preset, so unmount every rendered tree between tests to keep the jsdom document clean.
afterEach(() => {
  cleanup();
});

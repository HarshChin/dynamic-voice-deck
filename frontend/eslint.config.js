// @ts-check
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * ESLint 10 flat config for Dynamic Voice Deck.
 *
 * Formatting is owned entirely by Prettier; no stylistic/formatting rules are enabled here.
 * (typescript-eslint's `stylistic` preset contains no formatting rules since v6 — they were moved
 * to @stylistic — so it can coexist with Prettier without eslint-config-prettier.)
 *
 * The type-checked presets are the point of this config: the audio/WebSocket pipeline is almost
 * entirely async, so `no-floating-promises`, `no-misused-promises` and `await-thenable` are the
 * rules that actually protect it.
 */
export default tseslint.config(
  {
    ignores: ["dist/**", "coverage/**", "node_modules/**", "public/**", "src/assets/**"],
  },

  // This config file itself: plain JS, parsed by espree, no type information available.
  {
    name: "dvd/config-files-js",
    files: ["**/*.js"],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: globals.node,
    },
  },

  // Everything under src/, plus the TypeScript build config files.
  {
    name: "dvd/typescript",
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommendedTypeChecked,
      tseslint.configs.stylisticTypeChecked,
      reactHooks.configs.flat["recommended-latest"],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Async correctness — the reason this project pays for type-aware linting at all.
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      "@typescript-eslint/await-thenable": "error",
      "@typescript-eslint/require-await": "error",
      // CLAUDE.md §5: no `any`; narrow from `unknown` instead.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unnecessary-condition": "error",
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { fixStyle: "inline-type-imports", prefer: "type-imports" },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      "no-console": ["error", { allow: ["warn", "error"] }],
      eqeqeq: ["error", "smart"],
    },
  },

  // vite.config.ts runs in Node, not the browser, and is not a React module.
  {
    name: "dvd/vite-config",
    files: ["vite.config.ts"],
    languageOptions: {
      globals: globals.node,
    },
    rules: {
      "react-refresh/only-export-components": "off",
    },
  },

  // Tests: assertions on loosely typed fakes and mocks are normal here.
  {
    name: "dvd/tests",
    files: ["src/**/*.test.{ts,tsx}", "src/test-setup.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-unnecessary-condition": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      "@typescript-eslint/require-await": "off",
      "@typescript-eslint/unbound-method": "off",
      "react-refresh/only-export-components": "off",
    },
  },
);

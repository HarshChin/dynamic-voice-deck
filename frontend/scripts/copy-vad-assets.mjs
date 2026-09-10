/**
 * Copy the voice detector's runtime assets into `public/vad/`.
 *
 * The detector loads an ONNX model and an AudioWorklet at run time, and by default fetches them from
 * a CDN. That is wrong here twice over: the demo must work offline, and a microphone feature that
 * silently depends on a third party is a failure waiting for a conference network. Copying them into
 * `public/` makes them same-origin and pinned by the lockfile.
 *
 * Runs automatically after `npm install`.
 */
import { copyFile, mkdir, readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const target = join(here, "..", "public", "vad");
const sources = [
  join(here, "..", "node_modules", "@ricky0123", "vad-web", "dist"),
  join(here, "..", "node_modules", "onnxruntime-web", "dist"),
];

/** Suffixes the detector actually requests at run time. */
const WANTED = [".onnx", ".worklet.bundle.min.js", ".wasm", ".mjs"];

await mkdir(target, { recursive: true });
let copied = 0;
for (const from of sources) {
  for (const name of await readdir(from)) {
    if (WANTED.some((suffix) => name.endsWith(suffix))) {
      await copyFile(join(from, name), join(target, name));
      copied += 1;
    }
  }
}
console.log(`vad assets copied: ${String(copied)}`);

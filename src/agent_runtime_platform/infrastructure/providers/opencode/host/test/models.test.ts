/**
 * The catalog call against the real embedded host.
 *
 * Like `session.test.ts`, this starts the embedded host with the bundled model
 * catalog and never prompts, so it reaches no model service and needs no
 * credential. The catalog is pinned by the SDK version declared in
 * `../manifest.toml`; the private environment must be in place before the SDK
 * is imported, so the runner is loaded dynamically after `preparePrivateHome`.
 */

import { afterAll, beforeAll, describe, expect, test } from "bun:test";

import { preparePrivateHome } from "../src/isolation";

// A free model from the bundled snapshot; it has the effort variants the
// platform maps to `model_reasoning_effort`.
const MODEL = "opencode/space-bunny-free";

let listModels: typeof import("../src/session").listModels;
let home: ReturnType<typeof preparePrivateHome>;

beforeAll(async () => {
  home = preparePrivateHome();
  ({ listModels } = await import("../src/session"));
});

afterAll(() => {
  home.cleanup();
});

describe("listModels", () => {
  test("lists the bundled catalog in the shared shape", async () => {
    const models = await listModels();
    expect(models.length).toBeGreaterThan(0);
    for (const model of models) {
      expect(typeof model.id).toBe("string");
      expect(model.id).toContain("/");
      expect(typeof model.label).toBe("string");
      expect(typeof model.is_default).toBe("boolean");
      expect(typeof model.default_effort).toBe("string");
      expect(Array.isArray(model.efforts)).toBe(true);
      expect(model.efforts.every((effort) => typeof effort === "string")).toBe(true);
    }
    expect(models.filter((model) => model.is_default)).toHaveLength(1);
  }, 60_000);

  test("names the effort variants of a reasoning model", async () => {
    const models = await listModels();
    const preferred = models.find((model) => model.id === MODEL);
    expect(preferred?.efforts).toEqual(["low", "medium", "high", "xhigh", "max"]);
  }, 60_000);
});

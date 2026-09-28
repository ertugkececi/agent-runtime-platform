import { describe, expect, test } from "bun:test";
import { statSync } from "node:fs";

import { configDirectory, preparePrivateHome } from "../src/isolation";

describe("preparePrivateHome", () => {
  test("points the unset XDG roots at one private directory", () => {
    const env: Record<string, string | undefined> = {};
    const home = preparePrivateHome(env);
    try {
      expect(home.root).toBeDefined();
      const root = home.root ?? "";
      expect(env["XDG_CONFIG_HOME"]).toBe(`${root}/config`);
      expect(env["XDG_DATA_HOME"]).toBe(`${root}/data`);
      expect(env["XDG_CACHE_HOME"]).toBe(`${root}/cache`);
      expect(env["XDG_STATE_HOME"]).toBe(`${root}/state`);
      expect(statSync(root).mode & 0o777).toBe(0o700);
    } finally {
      home.cleanup();
    }
  });

  test("an environment that already provides roots wins", () => {
    const env: Record<string, string | undefined> = {
      XDG_CONFIG_HOME: "/private/config",
      XDG_DATA_HOME: "/private/data",
      XDG_CACHE_HOME: "/private/cache",
      XDG_STATE_HOME: "/private/state",
    };
    const home = preparePrivateHome(env);
    expect(home.root).toBeUndefined();
    expect(env["XDG_CONFIG_HOME"]).toBe("/private/config");
    home.cleanup();
  });

  test("fills in only what the environment left unset", () => {
    const env: Record<string, string | undefined> = { XDG_CONFIG_HOME: "/private/config" };
    const home = preparePrivateHome(env);
    try {
      expect(env["XDG_CONFIG_HOME"]).toBe("/private/config");
      expect(env["XDG_DATA_HOME"]).toBe(`${home.root}/data`);
    } finally {
      home.cleanup();
    }
  });
});

describe("configDirectory", () => {
  test("uses the environment's private config root when there is one", () => {
    expect(configDirectory({ XDG_CONFIG_HOME: "/private/config" }, "/tmp/fallback")).toBe(
      "/private/config/opencode",
    );
  });

  test("falls back to a private directory, never the user's config", () => {
    expect(configDirectory({}, "/tmp/fallback")).toBe("/tmp/fallback/opencode");
    expect(configDirectory({ XDG_CONFIG_HOME: "  " }, "/tmp/fallback")).toBe(
      "/tmp/fallback/opencode",
    );
  });
});

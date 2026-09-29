import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { applyPrivateUmask, configDirectory, dataDirectory, preparePrivateHome, redirectConsoleToStderr } from "../src/isolation";

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

  test("never falls back to the invoking user's home", () => {
    const env: Record<string, string | undefined> = { HOME: "/home/user" };
    const home = preparePrivateHome(env);
    try {
      expect(home.root).toBeDefined();
      for (const name of ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]) {
        expect(env[name]?.startsWith(home.root ?? "\0")).toBe(true);
        expect(env[name]).not.toContain("/home/user");
      }
    } finally {
      home.cleanup();
    }
  });
});

describe("applyPrivateUmask", () => {
  test("makes the files the host creates readable only by its user", () => {
    const root = mkdtempSync(join(tmpdir(), "agent-runtime-umask-"));
    const previous = applyPrivateUmask();
    try {
      const file = join(root, "probe");
      writeFileSync(file, "probe");
      expect(statSync(file).mode & 0o777).toBe(0o600);
    } finally {
      process.umask(previous);
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("redirectConsoleToStderr", () => {
  test("sends console writes to stderr, never stdout", () => {
    const originalLog = console.log;
    const originalInfo = console.info;
    const originalDebug = console.debug;
    const originalStderrWrite = process.stderr.write.bind(process.stderr);
    const originalStdoutWrite = process.stdout.write.bind(process.stdout);
    const stderrWrites: string[] = [];
    const stdoutWrites: string[] = [];
    process.stderr.write = ((chunk: string | Uint8Array) => {
      stderrWrites.push(String(chunk));
      return true;
    }) as typeof process.stderr.write;
    process.stdout.write = ((chunk: string | Uint8Array) => {
      stdoutWrites.push(String(chunk));
      return true;
    }) as typeof process.stdout.write;
    try {
      redirectConsoleToStderr();
      console.log("spawning process", { command: "mcp-server" });
      console.info("diagnostic");
      expect(stderrWrites.join("")).toContain("spawning process");
      expect(stderrWrites.join("")).toContain("mcp-server");
      expect(stderrWrites.join("")).toContain("diagnostic");
      expect(stdoutWrites).toEqual([]);
    } finally {
      console.log = originalLog;
      console.info = originalInfo;
      console.debug = originalDebug;
      process.stderr.write = originalStderrWrite as typeof process.stderr.write;
      process.stdout.write = originalStdoutWrite as typeof process.stdout.write;
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

describe("dataDirectory", () => {
  test("uses the environment's data root when there is one", () => {
    expect(dataDirectory({ XDG_DATA_HOME: "/private/data" }, "/tmp/fallback")).toBe(
      "/private/data/opencode",
    );
  });

  test("falls back to a private directory, never the user's data", () => {
    expect(dataDirectory({}, "/tmp/fallback")).toBe("/tmp/fallback/opencode");
    expect(dataDirectory({ XDG_DATA_HOME: "  " }, "/tmp/fallback")).toBe(
      "/tmp/fallback/opencode",
    );
  });
});

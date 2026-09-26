const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const html = fs.readFileSync(
  path.join(__dirname, "../src/agent_runtime_platform/static/index.html"),
  "utf8",
);
function functionSource(name, signature) {
  const escaped = signature.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const source = html.match(new RegExp(`(?:async )?function ${name}\\(${escaped}\\) \\{[\\s\\S]*?\\n      \\}`))?.[0];
  assert.ok(source, `${name} is present in the UI`);
  return source;
}

const runListSource = functionSource("roomRunsFromPayload", "payload");
const statusLabelSource = functionSource("roomRunLabel", "status");
const turnSplitSource = functionSource("splitRoomTurns", "turns");
const orderedIdsSource = functionSource("orderedRoomAgentIds", "");
const oldestFirstSource = functionSource("roomRunsOldestFirst", "payload");

test("room history accepts both list and wrapped API responses", () => {
  const helpers = new Function(`${runListSource}; return roomRunsFromPayload;`)();
  const runs = [{ id: "r1" }];
  assert.deepEqual(helpers(runs), runs);
  assert.deepEqual(helpers({ runs }), runs);
  assert.deepEqual(helpers({ items: runs }), runs);
});

test("newest-first API history is rendered oldest-first and preserves the newest status", () => {
  const ordered = new Function(`${runListSource}; ${oldestFirstSource}; return roomRunsOldestFirst;`)({});
  const newestFirst = [
    { id: "run-3", status: "running" },
    { id: "run-2", status: "completed" },
    { id: "run-1", status: "failed" },
  ];
  assert.deepEqual(ordered(newestFirst).map((run) => run.id), ["run-1", "run-2", "run-3"]);
  assert.equal(ordered(newestFirst).at(-1).status, "running");
});

test("room status labels cover queue lifecycle", () => {
  const label = new Function(`${statusLabelSource}; return roomRunLabel;`)();
  assert.equal(label("queued"), "Sırada");
  assert.equal(label("running"), "Çalışıyor");
  assert.equal(label("completed"), "Tamamlandı");
  assert.equal(label("failed"), "Başarısız");
});

test("moderator participant turn stays ordered before the distinct final summary", () => {
  const split = new Function(`${turnSplitSource}; return splitRoomTurns;`)();
  const result = split([
    { position: 2, agent_name: "Moderatör", is_moderator: true, phase: "moderator_summary" },
    { position: 1, agent_name: "Moderatör", is_moderator: true, phase: "participant" },
    { position: 0, agent_name: "Analist", is_moderator: false, phase: "participant" },
  ]);
  assert.deepEqual(result.participantTurns.map((turn) => turn.agent_name), ["Analist", "Moderatör"]);
  assert.equal(result.summaryTurn.phase, "moderator_summary");
});

test("participant order follows the reorderable room list", () => {
  const ordered = new Function("roomOrderList", `${orderedIdsSource}; return orderedRoomAgentIds;`)({
    querySelectorAll: () => [{ dataset: { agentId: "agent-b" } }, { dataset: { agentId: "agent-a" } }],
  });
  assert.deepEqual(ordered(), ["agent-b", "agent-a"]);
});
const monitorSource = functionSource("monitorRoomRun", "runId, roomId, generation");

test("room polling paints final data only while that room view is active", async () => {
  const state = { mode: "room", room: "room-a", run: "run-a", generation: 4, runs: [{ id: "run-a" }], statuses: [] };
  const harness = new Function("state", "request", "setTimeout", `
    let viewMode = state.mode, activeRoomId = state.room, activeRoomRunId = state.run;
    let viewGeneration = state.generation, roomRuns = state.runs;
    const renderRoomRuns = () => {};
    const showRoomRunState = (run) => state.statuses.push(run.status);
    ${monitorSource}
    return { monitor: () => monitorRoomRun("run-a", "room-a", 4), switchView: () => {
      viewMode = "chat"; viewGeneration += 1; activeRoomRunId = "";
    }, getRuns: () => roomRuns };
  `)(
    state,
    async () => ({ id: "run-a", status: "completed", final_answer: "Özet" }),
    async () => {},
  );
  await harness.monitor();
  assert.equal(harness.getRuns()[0].final_answer, "Özet");
  assert.deepEqual(state.statuses, ["completed"]);
});

test("a switched room view ignores a late poll response", async () => {
  const state = { mode: "room", room: "room-a", run: "run-a", generation: 4, runs: [{ id: "run-a", status: "running" }], statuses: [] };
  let finishRequest;
  const harness = new Function("state", "request", "setTimeout", `
    let viewMode = state.mode, activeRoomId = state.room, activeRoomRunId = state.run;
    let viewGeneration = state.generation, roomRuns = state.runs;
    const renderRoomRuns = () => {};
    const showRoomRunState = (run) => state.statuses.push(run.status);
    ${monitorSource}
    return { monitor: () => monitorRoomRun("run-a", "room-a", 4), switchView: () => {
      viewMode = "chat"; viewGeneration += 1; activeRoomRunId = "";
    }, getRuns: () => roomRuns };
  `)(
    state,
    () => new Promise((resolve) => { finishRequest = resolve; }),
    async () => {},
  );
  const polling = harness.monitor();
  while (!finishRequest) await new Promise(setImmediate);
  harness.switchView();
  finishRequest({ id: "run-a", status: "completed", final_answer: "stale" });
  await polling;
  assert.equal(harness.getRuns()[0].status, "running");
  assert.deepEqual(state.statuses, []);
});
const submitRoomSource = functionSource("submitRoomTask", "");

test("group task submit ignores a duplicate while its history preflight is pending", async () => {
  let finishPreflight;
  let postCalls = 0;
  const state = { monitored: [] };
  const request = async (_path, options = {}) => {
    if (options.method === "POST") {
      postCalls += 1;
      return { id: "run-1", status: "queued" };
    }
    return new Promise((resolve) => { finishPreflight = resolve; });
  };
  const messageInput = { value: "Review this design", disabled: false, focus() {} };
  const sendButton = { disabled: false };
  const submit = new Function("request", "messageInput", "sendButton", "state", `
    const setStatus = () => {};
    const renderRoomRuns = () => {};
    const showRoomRunState = () => {};
    const monitorRoomRun = (...args) => state.monitored.push(args);
    const chatModeButton = {}, roomModeButton = {};
    const document = { getElementById: () => ({}) };
    let activeRoomId = "room-1", activeRoomRunId = "", roomRunInFlight = false;
    let roomSubmissionUncertain = false, roomHistoryLoading = false, viewMode = "room", viewGeneration = 1;
    let roomRuns = [];
    ${runListSource}
    ${submitRoomSource}
    return submitRoomTask;
  `)(request, messageInput, sendButton, state);
  const first = submit();
  while (!finishPreflight) await new Promise(setImmediate);
  await submit();
  assert.equal(postCalls, 0, "second call must stop before posting");
  finishPreflight({ runs: [] });
  await first;
  assert.equal(postCalls, 1);
  assert.deepEqual(state.monitored, [["run-1", "room-1", 1]]);
});

test("preflight adopts the newest queued or running run without posting", async () => {
  let postCalls = 0;
  const state = { monitored: [], active: "" };
  const request = async (path, options = {}) => {
    if (options.method === "POST") {
      postCalls += 1;
      return { id: "unexpected", status: "queued" };
    }
    if (path.endsWith("/runs")) return { runs: [
      { id: "run-new", content: "Other task", status: "running" },
      { id: "run-old", content: "Earlier task", status: "queued" },
    ] };
    return { id: "run-new", content: "Other task", status: "running", turns: [] };
  };
  const messageInput = { value: "Review this design", disabled: false, focus() {} };
  const sendButton = { disabled: false };
  const submit = new Function("request", "messageInput", "sendButton", "state", `
    const setStatus = () => {};
    const renderRoomRuns = () => {};
    const showRoomRunState = (run) => { state.active = run.id; };
    const monitorRoomRun = (...args) => state.monitored.push(args);
    const chatModeButton = {}, roomModeButton = {};
    const document = { getElementById: () => ({}) };
    let activeRoomId = "room-1", activeRoomRunId = "", roomRunInFlight = false;
    let roomSubmissionUncertain = false, roomHistoryLoading = false, viewMode = "room", viewGeneration = 1;
    let roomRuns = [];
    ${runListSource}
    ${submitRoomSource}
    return { submitRoomTask, getRuns: () => roomRuns };
  `)(request, messageInput, sendButton, state);

  await submit.submitRoomTask();
  assert.equal(postCalls, 0);
  assert.equal(state.active, "run-new");
  assert.deepEqual(state.monitored, [["run-new", "room-1", 1]]);
  assert.deepEqual(submit.getRuns().map((run) => run.id), ["run-new"]);
});

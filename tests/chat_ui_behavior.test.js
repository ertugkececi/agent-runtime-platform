const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const html = fs.readFileSync(
  path.join(__dirname, "../src/agent_runtime_platform/static/index.html"),
  "utf8",
);
const formStart = html.indexOf(
  'document.getElementById("chat-form").addEventListener("submit", async (event) => {',
);
const formEnd = html.indexOf(
  '\n      });\n\n      messageInput.addEventListener("keydown"',
  formStart,
);
assert.ok(formStart >= 0 && formEnd > formStart, "chat form handler is present");
const callbackMarker = "async (event) => {";
const bodyStart = html.indexOf(callbackMarker, formStart) + callbackMarker.length;
const submitBody = html.slice(bodyStart, formEnd);
const poller = html.match(
  /async function monitorRun\(runId, requestedConversationId, generation\) \{[\s\S]*?\n      \}/,
)?.[0];
assert.ok(poller, "run poller is present");

function makeSubmit({ post, recovery }) {
  const state = {
    conversationId: "c1",
    generation: 1,
    activeRunId: "",
    uncertainSubmission: false,
    sendDisabled: false,
    status: "",
    postCalls: 0,
    monitored: [],
  };
  const messageInput = { value: "question", disabled: false, focus() {} };
  const sendButton = {};
  Object.defineProperty(sendButton, "disabled", {
    get: () => state.sendDisabled,
    set: (value) => { state.sendDisabled = value; },
  });
  const localStorage = { setItem() {}, removeItem() {} };
  const request = async (requestPath) => {
    if (requestPath.includes("/messages/async")) {
      state.postCalls += 1;
      return post();
    }
    return { messages: [{ id: "old", sender_type: "user", content: "old", run_id: "old-run" }] };
  };
  const loadConversation = async () => recovery();
  const setStatus = (status) => { state.status = status; };
  const showRunState = (status) => {
    state.status = status;
    state.sendDisabled = status === "queued" || status === "running";
  };
  const monitorRun = (...args) => state.monitored.push(args);
  const scope = new Function(
    "messageInput", "agentSelect", "sendButton", "request", "loadConversation",
    "showRunState", "setStatus", "monitorRun", "localStorage", "state",
    `let activeRunId = state.activeRunId;
     let conversationId = state.conversationId;
     let viewGeneration = state.generation;
     let uncertainSubmission = state.uncertainSubmission;
     let sendInFlight = false;
     const submit = async (event) => {${submitBody}};
     return {
       submit,
       switchContext: (id, generation) => {
         conversationId = id;
         viewGeneration = generation;
         activeRunId = "";
         sendInFlight = false;
         state.conversationId = id;
         state.generation = generation;
       },
       sync: () => {
         state.activeRunId = activeRunId;
         state.uncertainSubmission = uncertainSubmission;
       },
     };`,
  )(
    messageInput, { value: "agent" }, sendButton, request, loadConversation,
    showRunState, setStatus, monitorRun, localStorage, state,
  );
  return { state, messageInput, ...scope };
}

test("unknown async submission remains locked after finally and blocks repeat submit", async () => {
  const harness = makeSubmit({
    post: async () => { throw new Error("network interrupted"); },
    recovery: async () => null,
  });
  await harness.submit({ preventDefault() {} });
  harness.sync();
  assert.equal(harness.state.uncertainSubmission, true);
  assert.equal(harness.state.sendDisabled, true);
  await harness.submit({ preventDefault() {} });
  assert.equal(harness.state.postCalls, 1, "uncertain request is not submitted a second time");
});

test("successful recovery read avoids the old block-scope ReferenceError", async () => {
  const harness = makeSubmit({
    post: async () => { throw new Error("rejected"); },
    recovery: async () => ({ messages: [] }),
  });
  await assert.doesNotReject(harness.submit({ preventDefault() {} }));
  harness.sync();
  assert.equal(harness.state.uncertainSubmission, false);
  assert.equal(harness.state.sendDisabled, false, "a confirmed absent run can be retried");
});

test("second submit is ignored while the first async POST is in flight", async () => {
  let accept;
  const harness = makeSubmit({
    post: () => new Promise((resolve) => { accept = resolve; }),
    recovery: async () => ({ messages: [] }),
  });
  const first = harness.submit({ preventDefault() {} });
  while (!accept) await new Promise(setImmediate);
  await harness.submit({ preventDefault() {} });
  assert.equal(harness.state.postCalls, 1);
  accept({ id: "run-1", status: "queued" });
  await first;
  assert.equal(harness.state.monitored.length, 1);
});

test("late accept and terminal poll results cannot update a switched conversation", async () => {
  let releaseLoad;
  const submitHarness = makeSubmit({
    post: async () => ({ id: "run-1", status: "queued" }),
    recovery: () => new Promise((resolve) => { releaseLoad = resolve; }),
  });
  const submit = submitHarness.submit({ preventDefault() {} });
  while (!releaseLoad) await new Promise(setImmediate);
  submitHarness.switchContext("c2", 2);
  releaseLoad({ messages: [] });
  await submit;
  assert.equal(submitHarness.state.monitored.length, 0);

  const state = { activeRunId: "run-1", conversationId: "c1", generation: 1, statuses: [] };
  let releaseTerminalLoad;
  const poll = new Function(
    "request", "loadConversation", "showRunState", "setStatus", "setTimeout", "state",
    `let activeRunId = state.activeRunId;
     let conversationId = state.conversationId;
     let viewGeneration = state.generation;
     const monitorRun = (${poller});
     return {
       run: () => monitorRun("run-1", "c1", 1),
       switchContext: () => { activeRunId = ""; conversationId = "c2"; viewGeneration += 1; },
     };`,
  )(
    async () => ({ status: "completed" }),
    () => new Promise((resolve) => { releaseTerminalLoad = resolve; }),
    (status) => state.statuses.push(status),
    (status) => state.statuses.push(status),
    async () => {},
    state,
  );
  const polling = poll.run();
  while (!releaseTerminalLoad) await new Promise(setImmediate);
  poll.switchContext();
  releaseTerminalLoad({ messages: [] });
  await polling;
  assert.ok(!state.statuses.includes("completed"));
});

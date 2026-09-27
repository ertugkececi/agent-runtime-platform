const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const html = fs.readFileSync(
  path.join(__dirname, "../src/agent_runtime_platform/static/index.html"),
  "utf8",
);
const appScreenSource = html.match(
  /function setAppScreen\(screen,[\s\S]*?\n      \}/,
)?.[0];
assert.ok(appScreenSource, "screen navigation handler is present");

function makeNavigation() {
  const state = { mode: "chat", storedConversationId: "conversation-1", roomLoads: 0, chatLoads: 0 };
  const screens = {
    workspace: { hidden: false },
    agents: { hidden: true },
    create: { hidden: true },
  };
  const current = { hash: "" };
  const buttons = ["chat", "rooms", "agents", "new-agent"].map((screen) => ({
    dataset: { screen },
    attrs: {},
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; },
  }));
  const history = { pushState(_state, _title, url) { current.hash = url.slice(url.indexOf("#")); } };
  const setViewMode = (mode) => {
    state.mode = mode;
    if (mode === "room") state.roomLoads += 1;
    else state.chatLoads += 1;
  };
  const loadRoomView = () => { state.roomLoads += 1; };
  const loadConversation = () => { state.chatLoads += 1; };
  const handler = new Function(
    "state", "screens", "buttons", "current", "history", "window", "loadRoomView", "loadConversation",
    `let currentScreen = "chat", viewMode = state.mode, activeRoomId = "room-1";
     const setViewMode = (mode) => {
       viewMode = mode; state.mode = mode;
       if (mode === "room") state.roomLoads += 1;
       else state.chatLoads += 1;
     };
     const navigationButtons = buttons;
     const conversationWorkspace = screens.workspace;
     const agentsScreen = screens.agents;
     const newAgentScreen = screens.create;
     ${appScreenSource}
     return setAppScreen;`,
  )(state, screens, buttons, current, history, { location: current, history }, loadRoomView, loadConversation);
  return { state, screens, buttons, current, handler };
}

test("screen navigation separates management and creation while preserving conversation state", () => {
  const nav = makeNavigation();
  nav.handler("agents", { writeHistory: true });
  assert.equal(nav.screens.workspace.hidden, true);
  assert.equal(nav.screens.agents.hidden, false);
  assert.equal(nav.buttons[2].attrs["aria-current"], "page");
  assert.equal(nav.current.hash, "#agents");
  assert.equal(nav.state.storedConversationId, "conversation-1");

  nav.handler("new-agent");
  assert.equal(nav.screens.create.hidden, false);
  assert.equal(nav.screens.agents.hidden, true);
  assert.equal(nav.state.storedConversationId, "conversation-1");
});

test("chat and room navigation restores the matching screen and history", () => {
  const nav = makeNavigation();
  nav.handler("rooms");
  assert.equal(nav.state.mode, "room");
  assert.equal(nav.state.roomLoads, 1);
  assert.equal(nav.screens.workspace.hidden, false);
  nav.handler("chat");
  assert.equal(nav.state.mode, "chat");
  assert.equal(nav.state.chatLoads, 1);
  assert.equal(nav.screens.workspace.hidden, false);
});

test("refresh and browser history use explicit hash routes", () => {
  assert.match(html, /function screenFromHash\(\)/);
  assert.match(html, /window\.addEventListener\("hashchange", \(\) => setAppScreen/);
  assert.match(html, /data-screen="new-agent"/);
  assert.match(html, /localStorage\.getItem\("agentRuntimeConversationId"\)/);
  assert.match(html, /localStorage\.getItem\("agentRuntimeRoomId"\)/);
});

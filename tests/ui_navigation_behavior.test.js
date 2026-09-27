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
     let screenGeneration = 0;
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

const managedSelectionSource = html.match(
  /function selectManagedAgent\(agentId\) \{[\s\S]*?\n      \}/,
)?.[0];
assert.ok(managedSelectionSource, "agent management selection helper is present");

test("management selection changes the edit target without changing chat agent or conversation", () => {
  const state = { chatAgentId: "agent-chat", conversationId: "conversation-1", managedAgentId: "" };
  const select = new Function("state", `
    const agents = [{ id: "agent-chat" }, { id: "agent-other" }];
    const allAgents = agents;
    let managedAgentId = state.managedAgentId;
    const renderAgentCards = () => {};
    const renderEditAgent = () => {};
    ${managedSelectionSource}
    return (id) => { selectManagedAgent(id); state.managedAgentId = managedAgentId; };
  `)(state);
  select("agent-other");
  assert.equal(state.managedAgentId, "agent-other");
  assert.equal(state.chatAgentId, "agent-chat");
  assert.equal(state.conversationId, "conversation-1");
  assert.match(html, /let agent = allAgents\.find\(\(item\) => item\.id === \(managedAgentId \|\| agentSelect\.value\)\);/);
});

const feedbackSource = html.match(
  /function setAgentFormStatus\(message, isError = false, formScreen = currentScreen\) \{[\s\S]*?\n      \}/,
)?.[0];
assert.ok(feedbackSource, "agent form feedback helper is present");

test("create and edit feedback stays on its originating form after navigation", () => {
  for (const [formScreen, expectedId] of [["agents", "agent-edit-feedback"], ["new-agent", "agent-create-feedback"]]) {
    const feedback = { textContent: "", errors: [], classList: { toggle: (name, value) => feedback.errors.push([name, value]) } };
    const status = { textContent: "", classList: { toggle() {} } };
    const context = new Function("initialScreen", "feedback", "status", `
      let currentScreen = initialScreen;
      const agentEditFeedback = initialScreen === "agents" ? feedback : null;
      const agentCreateFeedback = initialScreen === "new-agent" ? feedback : null;
      const setStatus = (message, isError) => { status.textContent = message; };
      ${feedbackSource}
      return {
        setAgentFormStatus,
        leaveForm: () => { currentScreen = "rooms"; },
      };
    `)(formScreen, feedback, status);
    context.setAgentFormStatus("Kaydetme başladı", false, formScreen);
    assert.equal(status.textContent, "Kaydetme başladı");
    context.leaveForm();
    context.setAgentFormStatus("Kaydetme tamamlandı", false, formScreen);
    assert.equal(feedback.textContent, "Kaydetme tamamlandı");
    assert.equal(status.textContent, "Kaydetme başladı", "late form feedback does not overwrite another screen's status");
    assert.deepEqual(feedback.errors.at(-1), ["error", false]);
    assert.ok(html.includes(`id="${expectedId}"`));
  }

  assert.match(html, /setAgentFormStatus\("Ajan oluşturuluyor…", false, formScreen\)/);
  assert.match(html, /setAgentFormStatus\("Ajan ayarları kaydediliyor…", false, formScreen\)/);
});

const formContextSource = html.match(
  /function isCurrentFormContext\(formScreen, generation\) \{[\s\S]*?\n      \}/,
)?.[0];
assert.ok(formContextSource, "form screen generation guard is present");

test("creating an agent only navigates to chat if the create form context is still active", () => {
  const canNavigate = new Function("initialScreen", `
    let currentScreen = initialScreen;
    let screenGeneration = 4;
    ${formContextSource}
    const formScreen = currentScreen;
    const formGeneration = screenGeneration;
    currentScreen = "agents";
    screenGeneration += 1;
    return isCurrentFormContext(formScreen, formGeneration);
  `)("new-agent");
  assert.equal(canNavigate, false);
  assert.match(html, /const stillOnForm = isCurrentFormContext\(formScreen, formGeneration\)/);
  assert.match(html, /if \(stillOnForm && agent\.enabled\) \{[\s\S]*?setAppScreen\("chat"/);
  assert.match(html, /if \(screen !== previousScreen\) screenGeneration \+= 1/);
});


test("create completion preserves the existing chat if the user switches during agent loading", async () => {
  const context = new Function("initialScreen", `
    let currentScreen = initialScreen;
    let screenGeneration = 4;
    let conversationId = "conversation-existing";
    const storage = { conversationId: "conversation-existing" };
    ${formContextSource}
    const formScreen = currentScreen;
    const formGeneration = screenGeneration;
    return {
      leaveToChat: () => { currentScreen = "chat"; screenGeneration += 1; },
      finish: async (loadAgents) => {
        await loadAgents();
        const stillOnForm = isCurrentFormContext(formScreen, formGeneration);
        if (stillOnForm) {
          conversationId = "";
          delete storage.conversationId;
        }
        return { stillOnForm, conversationId, storedConversationId: storage.conversationId };
      },
    };
  `)("new-agent");
  let resolveLoad;
  const pending = context.finish(() => new Promise((resolve) => { resolveLoad = resolve; }));
  while (!resolveLoad) await new Promise(setImmediate);
  context.leaveToChat();
  resolveLoad();
  assert.deepEqual(await pending, {
    stillOnForm: false,
    conversationId: "conversation-existing",
    storedConversationId: "conversation-existing",
  }, "leaving during loadAgents preserves in-memory and persisted chat selection");
  assert.match(html, /await loadAgents\(\);\s*const stillOnForm = isCurrentFormContext\(formScreen, formGeneration\)/);
  assert.match(html, /const stillOnForm = isCurrentFormContext\(formScreen, formGeneration\);\s*setAgentFormStatus[\s\S]*?if \(stillOnForm && agent\.enabled\) \{\s*conversationId = "";\s*localStorage\.removeItem\("agentRuntimeConversationId"\)/);
});

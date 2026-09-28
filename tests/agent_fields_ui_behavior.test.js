const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const html = fs.readFileSync(
  path.join(__dirname, "../src/agent_runtime_platform/static/index.html"),
  "utf8",
);

test("agent create and edit forms expose persisted safe agent fields", () => {
  for (const id of [
    "agent-description", "model-provider", "agent-capabilities", "agent-tools", "agent-enabled",
    "edit-agent-name", "edit-agent-description", "edit-model-provider", "edit-agent-capabilities",
    "edit-agent-tools", "edit-agent-enabled",
  ]) {
    assert.match(html, new RegExp(`id="${id}"`), `missing ${id}`);
  }
  assert.match(html, /description: document\.getElementById\("agent-description"\)\.value/);
  assert.match(html, /tool_ids: selectedToolIds\("agent-tools"\)/);
  assert.match(html, /enabled: document\.getElementById\("agent-enabled"\)\.checked/);
  assert.match(html, /if \(enabled !== Boolean\(agent\.enabled\)\) changes\.enabled = enabled/);
  assert.match(html, /if \(provider !== agent\.model_provider\) changes\.model_provider = provider/);
});

test("agent tool picker only offers API-catalog entries approved as read-only", () => {
  assert.match(html, /toolCatalog\.filter\(\(tool\) => tool\.trusted_read_only\)/);
  assert.match(html, /request\("\/mcp\/tools"\)/);
  assert.doesNotMatch(html, /AGENT_RUNTIME_MCP_SERVERS|OPENAI_API_KEY|token_env/);
});

test("disabled agents stay in management while conversation selectors filter them out", () => {
  assert.match(html, /allAgents = fetchedAgents/);
  assert.match(html, /agents = fetchedAgents\.filter\(\(agent\) => agent\.enabled\)/);
  assert.match(html, /for \(const agent of allAgents\)/);
});

test("disabled creation stays on the creation screen and provider switching updates effort support", () => {
  assert.match(html, /if \(stillOnForm && agent\.enabled\)/);
  assert.match(html, /Ajan oluşturuldu fakat devre dışı/);
  assert.match(html, /updateEditEffortOptions\(\{ \.\.\.agent, model_provider: editProviderSelect\.value \}, effort\)/);
});

test("disabled managed Codex agents keep their model controls enabled after catalog refresh", () => {
  assert.match(html, /const editingAgent = allAgents\.find\(\(item\) => item\.id === \(managedAgentId \|\| agentSelect\.value\)\)/);
  assert.doesNotMatch(html, /const editingAgent = agents\.find/);
});

test("stale tool grants need explicit removal before tool edits and unrelated saves preserve them", () => {
  assert.match(html, /toolSelectionBaseline\.set\(containerId, selectedIds\.filter\(\(id\) => currentSet\.has\(id\)\)\.sort\(\)\)/);
  assert.match(html, /remove\.textContent = "İzni kaldır"/);
  assert.match(html, /function hasUnresolvedToolGrants\(containerId\)/);
  assert.match(html, /if \(toolsChanged && hasUnresolvedToolGrants\("edit-agent-tools"\)\)/);
  assert.match(html, /if \(toolsChanged\) changes\.tool_ids = toolIds/);
  assert.match(html, /Katalogda görünmeyen kayıtlı izinleri tek tek kaldırıp yeniden kaydet/);
});

test("falling back from a disabled chat agent clears its conversation and invalidates pending chat work", () => {
  assert.match(html, /const agentFallback = Boolean\(savedAgentId && savedAgentId !== agentSelect\.value\)/);
  assert.match(html, /if \(agentFallback\) \{[\s\S]*?chatContextGeneration \+= 1;[\s\S]*?viewGeneration \+= 1/);
  assert.match(html, /if \(agentFallback\) \{[\s\S]*?conversationId = "";[\s\S]*?localStorage\.removeItem\("agentRuntimeConversationId"\)/);
  assert.match(html, /Seçili ajan devre dışı\. Yeni etkin ajan için yeni sohbet hazır\./);
});

/**
 * Turning one bridge request into one OpenCode turn, and the turn back into
 * one result record. Pure functions only - the SDK never appears here.
 */

import type { TurnRequest } from "./protocol";

export interface ReplyResult {
  kind: "reply";
  content: string;
}

export interface HandoffResult {
  kind: "handoff";
  capability: string;
  task: string;
}

export type TurnResult = ReplyResult | HandoffResult;

/** One session message as the session API returns it, reduced to what we read. */
export interface SessionMessageLike {
  type?: unknown;
  time?: { created?: unknown };
  content?: unknown;
  outcome?: unknown;
}

const PREAMBLE = [
  "You are the agent in the conversation below. Follow the agent instructions.",
  "Answer in the user's language. Do not use tools, inspect files, or run commands.",
].join(" ");

const HANDOFF_INSTRUCTIONS = [
  "If one bounded subtask genuinely requires another agent, answer with a single JSON",
  'object and no other text: {"type":"handoff","capability":"<capability>","task":"<task>"}.',
  'Otherwise answer normally; {"type":"reply","content":"<reply>"} is also accepted.',
].join(" ");

/**
 * The agent's system prompt. `instructions` arrive from the adapter and may
 * carry the tool restriction and the handoff contract; the host adds only the
 * invariants it enforces itself.
 */
export function buildSystemPrompt(request: TurnRequest): string {
  const sections = [PREAMBLE, "", "Agent instructions:", request.instructions];
  if (request.allow_handoff) {
    sections.push("", HANDOFF_INSTRUCTIONS);
    const capabilities = request.remote_capabilities ?? [];
    if (capabilities.length > 0) {
      sections.push(
        `Administrator-configured remote A2A capabilities: ${capabilities.join(", ")}.`,
      );
    }
  }
  return sections.join("\n");
}

/**
 * The user prompt for the turn. The session is fresh on every call, so the
 * whole conversation travels as one JSON message, as the Codex provider does.
 */
export function buildPromptText(request: TurnRequest): string {
  return (
    "Here is the conversation history in chronological order as JSON. " +
    "Respond to the latest user message; earlier messages are context.\n" +
    JSON.stringify(request.history)
  );
}

/**
 * Interpret the model's final text.
 *
 * With `allow_handoff: false` the text is always a reply. With handoffs
 * allowed, a handoff envelope becomes a handoff; anything else stays a reply,
 * including a reply envelope.
 */
export function interpretFinalText(
  text: string | undefined,
  allowHandoff: boolean,
): TurnResult {
  const content = text ?? "";
  if (!allowHandoff) return { kind: "reply", content };

  const envelope = parseEnvelope(content);
  if (!envelope) return { kind: "reply", content };

  if (envelope["type"] === "handoff") {
    const capability = envelope["capability"];
    const task = envelope["task"];
    if (isNonEmptyString(capability) && isNonEmptyString(task)) {
      return { kind: "handoff", capability: capability.trim(), task: task.trim() };
    }
    return { kind: "reply", content };
  }
  if (envelope["type"] === "reply") {
    const reply = envelope["content"];
    if (typeof reply === "string") return { kind: "reply", content: reply };
  }
  return { kind: "reply", content };
}

/**
 * The final assistant text of a session: the newest assistant message that has
 * text, with its text parts in order.
 */
export function extractFinalText(messages: readonly SessionMessageLike[]): string | undefined {
  const assistants = messages.filter((message) => message.type === "assistant");
  assistants.sort((left, right) => created(right) - created(left));
  for (const message of assistants) {
    const text = textOf(message);
    if (text !== undefined) return text;
  }
  return undefined;
}

/**
 * The outcome of the turn: the newest idle message the session wrote. A turn
 * that did not end in success is an error, not an empty reply.
 */
export function turnOutcome(
  messages: readonly SessionMessageLike[],
): "succeeded" | "failed" | "interrupted" | undefined {
  const idle = messages
    .filter((message) => message.type === "idle")
    .sort((left, right) => created(right) - created(left))[0];
  if (!idle) return undefined;
  if (idle.outcome === "succeeded" || idle.outcome === "failed" || idle.outcome === "interrupted") {
    return idle.outcome;
  }
  return "failed";
}

function created(message: SessionMessageLike): number {
  const value = message.time?.created;
  return typeof value === "number" ? value : 0;
}

function textOf(message: SessionMessageLike): string | undefined {
  if (!Array.isArray(message.content)) return undefined;
  const parts = message.content.filter(
    (part): part is { type: "text"; text: string } =>
      typeof part === "object" &&
      part !== null &&
      (part as { type?: unknown }).type === "text" &&
      typeof (part as { text?: unknown }).text === "string",
  );
  if (parts.length === 0) return undefined;
  return parts.map((part) => part.text).join("");
}

function parseEnvelope(text: string): Record<string, unknown> | undefined {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{")) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return undefined;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return undefined;
  return parsed as Record<string, unknown>;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

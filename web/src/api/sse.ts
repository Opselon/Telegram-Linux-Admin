/**
 * SSE subscription to /api/v1/events/stream (auth via session cookie).
 * Server sends JSON frames `{kind, ...payload}`; keepalive comments are
 * ignored by EventSource automatically.
 */
import type { StreamEvent } from "./types";

type StreamListener = (event: StreamEvent) => void;

let source: EventSource | null = null;
const listeners = new Set<StreamListener>();
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function connect(): void {
  if (source) return;
  source = new EventSource("/api/v1/events/stream");

  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data) as StreamEvent;
      for (const listener of listeners) listener(event);
    } catch {
      // Malformed frame — ignore.
    }
  };

  source.onerror = () => {
    source?.close();
    source = null;
    if (listeners.size > 0 && reconnectTimer === null) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, 5000);
    }
  };
}

function disconnect(): void {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  source?.close();
  source = null;
}

/** Opens the stream on first subscriber, closes when the last unsubscribes. */
export function subscribeStream(listener: StreamListener): () => void {
  listeners.add(listener);
  connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) disconnect();
  };
}

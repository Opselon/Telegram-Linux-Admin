/** Formatting helpers (locale-aware, no invented data). */

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatDateTime(epochSeconds: number | string | null | undefined): string {
  if (epochSeconds === null || epochSeconds === undefined || epochSeconds === "") return "—";
  const ms = typeof epochSeconds === "number"
    ? epochSeconds * (epochSeconds > 1e12 ? 1 : 1000)
    : Date.parse(epochSeconds);
  if (Number.isNaN(ms)) return String(epochSeconds);
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function timeAgo(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 60) return "<1m";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

export function formatPercent(value: number | undefined | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return "—";
  return `${Math.round(value)}%`;
}

export function truncateMiddle(text: string, max = 60): string {
  if (text.length <= max) return text;
  const half = Math.floor((max - 1) / 2);
  return text.slice(0, half) + "…" + text.slice(-half);
}

/** Trigger a browser download for a Blob received from the API. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

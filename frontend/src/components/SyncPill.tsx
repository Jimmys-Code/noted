import type { SyncStatus } from "../api";
import { api } from "../api";

function fmtAgo(ts: number, now: number): string {
  if (!ts) return "—";
  const d = Math.max(0, now - ts);
  if (d < 1500) return "just now";
  if (d < 60_000) return `${Math.floor(d / 1000)}s ago`;
  if (d < 3_600_000) return `${Math.floor(d / 60_000)}m ago`;
  return `${Math.floor(d / 3_600_000)}h ago`;
}

export default function SyncPill({ status }: { status: SyncStatus | null }) {
  if (!status) return null;
  if (!status.enabled) {
    return (
      <button className="pill sync-pill off" title="Sync disabled — add ~/.config/noted/sync.toml">
        ⊘ LOCAL
      </button>
    );
  }
  let cls = "ok";
  let label = "SYNCED";
  if (status.last_error) { cls = "err"; label = "OFFLINE"; }
  else if (status.in_flight) { cls = "wait"; label = "SYNCING"; }
  else if (status.pending > 0) { cls = "wait"; label = `±${status.pending}`; }

  const now = status.server_ts || Date.now();
  const title = status.last_error
    ? `Sync error: ${status.last_error}`
    : `Last pull ${fmtAgo(status.last_pull_ts, now)} · push ${fmtAgo(status.last_push_ts, now)}` +
      (status.pending ? ` · ${status.pending} pending` : "");

  return (
    <button
      className={`pill sync-pill ${cls}`}
      onClick={() => api.syncNow()}
      title={title + " — click to sync now"}
    >
      ⟳ {label}
    </button>
  );
}

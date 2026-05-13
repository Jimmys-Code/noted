# noted-sync

Server-side sync for [noted](../). Single-user, shared-token. Local Electron
app reads + writes its own SQLite for snappy UX, then ferries deltas to/from
this server in the background.

## Data model

- Records (folders + notes) are uniquely identified by **UUID** (not the
  legacy local autoincrement `id`).
- Every record has `updated_at` in **ms since epoch**.
- Deletes are tombstones — the row stays with `deleted_at` set, so other
  devices learn about the delete on their next pull.
- Conflict resolution: **last-write-wins by `updated_at`**. On a tie, server
  keeps its version (deterministic).

## Endpoints

All require `Authorization: Bearer <NOTED_SYNC_TOKEN>` except `/health`.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health`        | Liveness + server time. |
| `GET`  | `/sync/state`    | Counts + DB size. Useful for "first ever sync — should I pull from since=0 or start fresh?". |
| `GET`  | `/sync/changes?since=<ms>` | Pull: returns every record with `updated_at > since`, alive and tombstoned. Response carries `server_ts` — client stores this as next `since`. |
| `POST` | `/sync/push`     | Upload a batch of `{folders: [...], notes: [...]}`. Per-record LWW. Returns `accepted_*` / `rejected_*` UUID lists. |
| `GET`  | `/sync/search?q=<query>&limit=<n>` | Substring scan across alive notes (title + body). For agent-comms RAG integration. |

## Client sync loop (Phase 1)

```
on app open:
    GET /sync/changes?since=<last_sync_ts>
    apply changes locally (LWW against local updated_at)
    store response.server_ts as new last_sync_ts

on local edit:
    write to local DB immediately, update local updated_at
    debounce 5s
    POST /sync/push with the edited records
    for rejected uuids: pull them and reconcile

every 30s while app open:
    GET /sync/changes?since=<last_sync_ts>
    (cheap — usually empty)
```

## Deployment

Designed to run on the same droplet as agent-comms, behind nginx at `/noted/`.

### systemd unit

See [`noted-sync.service`](./noted-sync.service). Copy to
`/etc/systemd/system/noted-sync.service`, set the token, enable + start:

```bash
sudo cp sync-server/noted-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now noted-sync
sudo systemctl status noted-sync
```

### nginx vhost snippet

Add inside your existing `server { ... }` block (alongside `/comms/`):

```nginx
location = /noted { return 301 /noted/; }
location /noted/ {
    proxy_pass http://127.0.0.1:8770/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Notes can be large — bump body cap. Adjust as needed.
    client_max_body_size 20M;
}
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

### Token

Generate once, set in the systemd unit's `Environment=NOTED_SYNC_TOKEN=...`,
and store the same token in your client config. Keep it different from the
agent-comms token — different blast radius, different sensitivity.

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

## Client-side migration notes (for the Electron + Python app under `../backend/`)

This is the work to do next, NOT shipped by this PR. Sketched here so the
contract is clear.

### Local schema additions

```sql
ALTER TABLE folders ADD COLUMN uuid TEXT;
ALTER TABLE folders ADD COLUMN updated_at INTEGER;  -- currently only on notes
ALTER TABLE folders ADD COLUMN deleted_at INTEGER;
ALTER TABLE notes   ADD COLUMN uuid TEXT;
ALTER TABLE notes   ADD COLUMN deleted_at INTEGER;
-- backfill: set uuid = lower(hex(randomblob(16))) for every existing row;
-- set folders.updated_at = created_at for legacy rows.

CREATE TABLE sync_state (
    key   TEXT PRIMARY KEY,   -- e.g. 'last_sync_ts'
    value TEXT
);
```

The integer `id`s stay (the Electron UI uses them). `uuid` is added as the
"sync identity" — two devices' rows for the same logical note share a `uuid`
but probably have different local `id`s.

### Client sync module

A tiny `frontend/src/sync.ts`:

- On note save: write local, set `updated_at = Date.now()`, schedule push (5s debounce).
- On app open: `GET /sync/changes?since=local.sync_state.last_sync_ts`, apply LWW, store new `server_ts`.
- Background poll: every 30s while app focused.
- Optionally: `navigator.onLine` listener for retry on reconnect.

Conflict reconciliation when pulling:

```
for incoming in response.folders + response.notes:
    local = find_by_uuid(incoming.uuid)
    if not local:
        insert (or skip if incoming.deleted_at set and not local)
    elif incoming.updated_at > local.updated_at:
        update local
    else:
        keep local (or push it on next push since server's behind)
```

### Token

Read from a config file (e.g. `~/.config/noted/sync.toml`):

```toml
server_url = "https://jimmyspianotuning.com.au/noted"
token      = "..."
```

## Phase 2 ideas (not in this PR)

- FTS5 search instead of `LIKE`. Cheap upgrade if the substring search gets slow.
- Attachments (binary blobs in notes). Mirror the agent-comms attachment table.
- Per-device device_id sent on push, server stores `last_seen` per device for UI ("synced from laptop 2 min ago").
- WebSocket / SSE push so other clients get updates without polling. Polling at 30s is fine until it isn't.
- Hookup to agent-comms: `comms notes-search <query>` subcommand that calls `/noted/sync/search` and renders matches. Or expose as a tool on the agent control side so agents can RAG without leaving the comms surface.

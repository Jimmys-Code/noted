<!-- noted-skill version: 1 -->

# Noted — agent skill

You can read and modify the operator's personal notes via the noted-sync HTTP API. This skill makes you fully equipped — paste this into any chat and you can browse, search, and edit notes immediately.

**Auth:**
```
TOKEN=0a52e664ea59833cc8bade87d0d70197187e9552d1ab309d
BASE=https://jimmyspianotuning.com.au/noted
```

All requests need `-H "Authorization: Bearer $TOKEN"`.

---

## Behavioral rules (read before doing anything)

These are non-negotiable. The operator wrote these.

### 1. Confirm the WRITE PLAN, not each write
Read endpoints (`GET …`) need no confirmation — fetch freely. For any **write** (POST/DELETE), describe the full plan once in chat ("I'll mark notes A and B as testing and append a checklist item to C"), get one approval, then execute. Don't ping-pong asking after each call.

If mid-execution something changes (e.g. a replace returns 422 with candidates), pause and re-confirm before retrying.

### 1b. NEVER set status='done' — that's operator-only

The operator is QC. They flip notes to `done` themselves after verifying on their device. When you finish work on an issue, set status to **`testing`**, NOT `done`. The server enforces this — `POST /sync/note/{uuid}/status` with `{"status":"done"}` returns 403 with hint to use 'testing'. Same for create with status='done'.

This is intentional: `done` = "operator-verified working"; `testing` = "agent claims it's working, awaiting operator verification". Don't blur them. If the operator explicitly says "mark it done" in chat, that's their override — they're acting AS the operator and you can pass `?as_operator=true` to honor that.

### 2. Match the operator's writing style — terse
The operator's notes are short. Look at a few adjacent notes in the same folder before writing anything new. Specifically:
- No headings unless the content is genuinely multi-section
- No paragraph-long explanations — bullet list if a list, single sentence if a fact, title-only if the content lives in the title
- Never add an AI-style preamble ("Here's a note about…", "I've added…")
- Never explain what the note is — the note IS the explanation
- If you have analysis to share, it goes in chat, not in a note (unless explicitly asked)

### 3. Status synonym mapping
The operator speaks naturally. Translate to the 5 allowed statuses:

| Operator says | Set status to |
|---|---|
| logged it / found a bug / problem / issue | `open` |
| working on / picked up / started / looking at | `in-progress` |
| ready to test / try this / can you try | `testing` |
| closed / resolved / fixed / shipped / complete / done / wrap | `testing` (NOT `done`! — operator-only, see rule 1b) |
| thought / idea / what if / maybe / consider | `idea` |
| reopen / unfixed / broken again / actually not fixed | `open` (or `in-progress` if work is ongoing) |
| plain note / not an issue / just remember | `null` (clear status) |

If genuinely ambiguous, ask: *"setting status to X — confirm?"* One sentence, not a dialog.

### 4. Prefer append over rewrite for ongoing notes
When the operator says "add to this note" / "log that you tried X", use `/append`. Don't fetch + rewrite — it churns the body and creates merge surprises.

### 5. Single-call writes — use them
Never do the read-modify-push dance. Use `/sync/note/{uuid}/status`, `/append`, `/replace` directly. Each is atomic and bumps the parent folder for you.

### 6. Prefer AI metadata over raw fields in list views
Every list endpoint (`/sync/recent`, `/sync/issues`, `/sync/project/{name}`, `/sync/search`, `/sync/folders?include_recent=N`) returns AI-generated metadata inline per note:
- `ai_title` — concrete, 2-4 word title. Prefer this over `title` when displaying (user's `title` is often a generic stub).
- `ai_summary` — one-sentence breadcrumb. Show it under the title.
- `ai_tags` — kebab-case array (e.g. `['vim', 'ios', 'sync']`). Useful for filtering/grouping.
- `ai_keypoints` — punchy bullets. Surface these when listing issues so you know what was tried + what's pending without fetching the body.
- `ai_status` — `'ok'` (metadata ready), `'streaming'` (title + summary arriving, deeper fields pending), `'pending'` (worker hasn't picked up yet), `'skipped'` (stub note < 30 chars), `'failed'` (worker gave up — check ai_error on full GET).

Don't fetch `/sync/note/{uuid}` for the full body unless the agent ACTUALLY needs the prose — `ai_summary` + `ai_keypoints` usually answer "what's this note about?" in 100x less context.

`ai_tldr` is NOT in list views (too long) — fetch the full note when diving in.

---

## The 10 endpoints

### Reads

```bash
# 1. Most-recent across everything — "what did I just write?"
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/recent?limit=10&sort=updated_at"
# sort=created_at for "most recently captured" (vs edited)

# 2. Folders dashboard — list of folders + their N most recent notes
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/folders?include_recent=3"
# Add ?kind=project for project folders only, ?active=true to skip archived

# 3. Open a project (or any folder) — 3 buckets in one call
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/project/Noted"
# Folder name is case-insensitive. Returns:
#   folder: {…}
#   issues:  [notes with status in open/in-progress/testing]   ← actionable
#   ideas:   [notes with status='idea']                         ← parked
#   recent:  [done + plain notes]                               ← history
# Pass ?body=true to inline note bodies.

# 4. Cross-project issues — all actionable work, grouped by project
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/issues"
# Returns {total_issues, total_ideas, groups: [{folder, issues[], ideas[]}, …]}
# Add ?project_only=false to include general folders too.

# 5. Read one note (full body)
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/note/{uuid}"

# 6. Substring search — "find my latest note about X"
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/search?q=ios+sync&sort=created_at&limit=5"
# Add ?folder={uuid}&status=open,in-progress for scoped searches.
```

### Writes

```bash
# 7. Create a note — server generates uuid + timestamps
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"folder":"Noted","title":"vim cursor jumps","body":"happens after :w when…","status":"open"}' \
  "$BASE/sync/note"
# folder is name OR uuid OR null (uncategorized)

# 8. Change status
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"done"}' "$BASE/sync/note/{uuid}/status"
# Pass {"status":null} to clear (revert to plain note)

# 9. Append text to body — server adds newline separator if needed
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"- tried disabling vim plugin: did not help"}' \
  "$BASE/sync/note/{uuid}/append"

# 10. Replace text in body — 3-layer fuzzy matching (exact → whitespace → fuzzy@0.8)
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"find":"- [ ] write tests","replace":"- [x] write tests"}' \
  "$BASE/sync/note/{uuid}/replace"
# On 200: response includes match_type ('exact'|'whitespace'|'fuzzy') + similarity.
# On 422: response has {candidates: [...]} with top-3 close blocks — adjust your `find` and retry.

# 11. Delete (tombstone — syncs the delete to other devices)
curl -sS -X DELETE -H "Authorization: Bearer $TOKEN" "$BASE/sync/note/{uuid}"
```

---

## Common workflows

**"What are my open issues on the Noted project?"**
```bash
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/project/Noted"
# → look at .issues[]
```

**"Find the issue about ios preview"**
```bash
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/search?q=ios+preview&status=open,in-progress"
```

**"Mark that issue as done, open the next one"**
```bash
# 1. Status change
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"done"}' "$BASE/sync/note/{uuid}/status"
# 2. Next issue is already in your cached project view — read body of the next one:
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/note/{next_uuid}"
```

**"It's not fixed — reopen and log that we tried X"**
```bash
# Plan: reopen + append. Confirm with operator once, then:
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"in-progress"}' "$BASE/sync/note/{uuid}/status"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"- tried X: did not fix it"}' "$BASE/sync/note/{uuid}/append"
```

**"What did I just write down on my phone?"**
```bash
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/recent?sort=created_at&limit=5"
```

**"Any ideas on the Noted project?"**
```bash
# You already have it from /sync/project/Noted — look at .ideas[]
# If not cached, re-fetch:
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/sync/project/Noted"
```

---

## Status values (the only 5)

`idea`, `open`, `in-progress`, `testing`, `done` — or `null` for plain notes. The API will 422 on any other value.

Folders have a `kind` (`general` or `project`) and an `active` boolean. Only project folders show up in `/sync/issues` by default.

---

## When something fails

- **404 on `/sync/project/{name}`**: folder doesn't exist by that name or uuid. Use `/sync/folders?kind=project` to list available projects.
- **422 on `/sync/note/{uuid}/replace`**: no fuzzy match. Read the `candidates` array in the response — those are the top-3 close blocks. Pick one, fix your `find` string, retry.
- **422 on create with status**: only the 5 statuses above are valid. Synonyms must be mapped client-side per the table.
- **403 `operator_only_status` on status='done'**: by design — set `testing` instead. See rule 1b. If the operator explicitly told you to mark it done, pass `?as_operator=true` to honor their override.
- **404 on writes after create**: uuid is wrong, OR you tried to use the URL-formatted UUID with dashes when the server returns hex (no dashes). They both work for routing, so this is usually a typo.

---

<!--
Future features may add:
- folder metadata (machine tags, git remote, etc.)
- per-agent attribution headers
- webhook integrations
A version bump (noted-skill version: 2) at the top will signal the skill changed.
-->

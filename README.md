# OpenCode v1 to v2 session migration

`migrate_v1_to_v2.py` copies OpenCode 1.x chat history into a database OpenCode 2.0.18 can open. It does this outside the app, so the beta build is never started and the broken beta `workspace` table is never copied.

Use it when Desktop starts on a fresh `opencode.db` but the old conversations are still in `opencode.db.pre-v2-fix`. That happens when an earlier V2 beta stamped `kv.migration.v1-v2` as `completed`, then OpenCode 1.18 kept writing `session` / `message` / `part` rows that 2.0.18 does not read.

## Quick path

1. Close OpenCode Desktop so `opencode-cli.exe` is not running.
2. From the data directory, build the migrated file:

```powershell
python migrate_v1_to_v2.py
```

3. Install it over the live database:

```powershell
python migrate_v1_to_v2.py --apply
```

4. Start Desktop and open the project folder the sessions came from.

`--apply` refuses to run while OpenCode is still running. It also saves the current `opencode.db` as `opencode.db.before-v1-apply-<timestamp>` before replacing it.

## What it writes

| Source | Destination |
|---|---|
| `session`, `message`, `part` | `session_v2`, `session_message` |
| Folder path | A `project` row for that directory, reusing one that already exists |
| Nothing from the old `workspace` table | Left empty |

The original backup is opened read-only. The default output is `opencode.v1-migrated.db`, cloned from the working 2.0.18 schema so providers and the test session stay intact.

Child and subagent sessions are imported with their `parent_id`. The project list shows the parent conversations.

## Options

| Flag | Effect |
|---|---|
| `--source` | V1 backup. Default: `opencode.db.pre-v2-fix` |
| `--base` | Working database used only as the schema clone |
| `--dest` | Output file. Default: `opencode.v1-migrated.db` |
| `--limit N` | Import only the oldest N v1 sessions |
| `--force` | Replace an existing destination file |
| `--apply` | Rebuild the destination, then replace `opencode.db` |

Requires Python 3. No third-party packages.

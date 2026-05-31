# scheduled-recheck — wait for next CDT run, auto-close DRGN on success

Solves a common NOC pattern: someone ack-s a transient Databricks failure
(typically `ConcurrentAppendException`-style chronic), and the next scheduled
run is expected to succeed in 5–30 minutes. Instead of writing a fresh check
loop each time, schedule a recheck and let it close the DRGN automatically.

This sits next to `auto-close` and **re-uses its `closure.py` library** — the
field IDs, resolution values, and Jira transition logic are the same battle-tested
code. The only new piece here is the persistent state file + macOS launchd glue.

## Lifecycle

```
schedule  →  state.json (status: pending)
     ↓
fire_at_iso reached
     ↓
run-pending    (manual, cron, or launchd)
     ↓
CDT check: did `job_name` have a successful run after `after_iso`?
     ↓
       success         failed             pending
         ↓               ↓                   ↓
  close DRGN      mark task failed      bump attempts,
  (auto-close     log on_failure        leave pending,
   library)       message               try again next run-pending
         ↓
  status: done
```

Each task is JSON in `state.json`:

```json
{
  "id": "DRGN-17897-c0ffee",
  "drgn": "DRGN-17897",
  "pd": "Q2F39BSFK5L3V2",
  "check_type": "cdt-job-success-after",
  "check_args": {
    "job_name": "asra_split_trx_header_fact",
    "after_iso": "2026-05-31T05:11:00Z"
  },
  "fire_at_iso": "2026-05-31T05:58:00Z",
  "on_success": {
    "action": "close-drgn",
    "resolution": "rvsp",
    "reference": ["DSSD-31131"],
    "append": "Igor performed manual repair at 05:11 UTC",
    "runbook_url": null,
    "sla": "no"
  },
  "on_failure": {"action": "notify", "message": "..."},
  "status": "pending",
  "attempts": 0
}
```

## Commands

### `schedule` — add a new task

```bash
python scheduled_recheck.py schedule \
    --drgn DRGN-17897 \
    --pd Q2F39BSFK5L3V2 \
    --job asra_split_trx_header_fact \
    --fire-after-min 12 \
    --on-success-resolution rvsp \
    --on-success-reference DSSD-31131 \
    --on-success-append "Igor performed manual repair at 05:11 UTC"
```

Required flags: `--drgn`, `--job`, and one of `--fire-after-min` / `--fire-at`.

The `--after-iso` (or default: now) is the threshold a success run must START
*after* — this is what makes the check safe (an old success doesn't count).

### `list` — show tasks

```bash
python scheduled_recheck.py list
python scheduled_recheck.py list --status pending
python scheduled_recheck.py list --status done
```

### `show` / `cancel`

```bash
python scheduled_recheck.py show DRGN-17897-c0ffee
python scheduled_recheck.py cancel DRGN-17897-c0ffee
```

### `run-pending` — process due tasks

```bash
# Manual
python scheduled_recheck.py run-pending

# Cron-friendly (no "no tasks" line, no Jira writes if --dry-run)
python scheduled_recheck.py run-pending --quiet
python scheduled_recheck.py run-pending --dry-run
```

For each task whose `fire_at_iso` ≤ now and `status == "pending"`:

- Calls CDT `find_batch_job` for `job_name`
- Looks at the most recent run that started AT/AFTER `after_iso`
- If `success` → closes DRGN via `closure.close_drgn()` (same library as `auto-close`)
- If `failed/cancelled` → marks task `failed` (no auto-escalation; logs `on_failure.message`)
- If `running/pending/no-data` → bumps `attempts`, leaves task pending; will retry next run

Comment composed from: default base (Auto / RvSP wording) + CDT run timestamps +
cross-references + custom append.

### `install-launchd` (Mac) — auto-run every 5 minutes

```bash
python scheduled_recheck.py install-launchd                # 5-min interval
python scheduled_recheck.py install-launchd --interval-min 3
python scheduled_recheck.py uninstall-launchd
```

Generates a `.plist` in `~/Library/LaunchAgents/com.master.noc-scheduled-recheck.plist`,
copies all keys from `noc-toolkit/.env` into the plist's `EnvironmentVariables`
so the spawned process has the API tokens, and `launchctl load -w`-s it.

Logs go to `tools/scheduled-recheck/launchd-logs/{stdout,stderr}.log`.

### Cron alternative (Linux, or Mac without launchd)

```cron
*/5 * * * * cd /Users/master/noc-toolkit/tools/scheduled-recheck && /usr/local/bin/python3.11 scheduled_recheck.py run-pending --quiet >> /tmp/scheduled-recheck.log 2>&1
```

## Why not use `auto-close scan` for this?

`auto-close scan` looks at ALL acknowledged PD incidents every run — broad
sweep, polled every 30 min. It only closes whitelisted jobs (asra family).

`scheduled-recheck` is the opposite: targeted, time-bounded, one-task-per-incident,
explicit on-success-* parameters, can close any DRGN (no whitelist). Both can
coexist; they don't step on each other (each has its own state).

## Files

- `scheduled_recheck.py` — CLI (schedule, list, show, cancel, run-pending, install/uninstall-launchd)
- `state.py` — JSON state store with atomic writes
- `state.json` — gitignored, generated on first `schedule`
- `launchd-logs/` — gitignored, generated on first `install-launchd`

# Durable Job Recovery

This example walks through a realistic recovery scenario: a calling agent (Claude
Code, Codex, Cursor, etc.) asks Codex for a second opinion, the parent tool call
times out, and the agent recovers the job on its next turn.

Every flag shown here is real and matches `crossagent`'s argparse surface.

## Scenario

You are deciding whether to use a state machine or a choreography for a new
order-processing workflow. You ask Codex for an evidence-backed critique and
start a durable job.

## Step 1: Start the job

```bash
crossagent start \
  --agent codex \
  --name "order-workflow-design" \
  --cwd "$PWD" \
  --max-runtime 1800 \
  --prompt-file /tmp/decision.md \
  --json
```

Expected stdout:

```json
{
  "schema_version": 1,
  "job_id": "job_20260718T120000_a1b2c3d4",
  "status": "running",
  "advisor": "codex",
  "started_at": "2026-07-18T12:00:00Z"
}
```

Retain the `job_id` — it is your only handle to the running job.

## Step 2: Poll while the parent limits allow

```bash
crossagent wait job_20260718T120000_a1b2c3d4 --timeout 45 --json
```

If the job is still active:

```json
{
  "schema_version": 1,
  "job_id": "job_20260718T120000_a1b2c3d4",
  "status": "running",
  "elapsed_seconds": 32,
  "idle_seconds": 10,
  "last_event": "item.completed",
  "updated_at": "2026-07-18T12:00:32Z"
}
```

`elapsed_seconds` measures wall time since start; `idle_seconds` measures time
since the advisor's last output. `running` is not a failure — do other work and
poll again.

## Step 3: Parent times out

The calling agent's tool-call limit expires. The agent's next message contains
the saved `job_id`. The agent **must not** start a new `crossagent start` for
the same decision — it recovers.

## Step 4: Check status

```bash
crossagent status job_20260718T120000_a1b2c3d4 --json
```

If the job completed while the parent was away:

```json
{
  "schema_version": 1,
  "job_id": "job_20260718T120000_a1b2c3d4",
  "status": "succeeded",
  "elapsed_seconds": 187,
  "idle_seconds": 3,
  "last_event": "succeeded",
  "updated_at": "2026-07-18T12:03:07Z"
}
```

## Step 5: Get the result

```bash
crossagent result job_20260718T120000_a1b2c3d4
```

This prints **only the advisor's answer** to stdout. Pipeline it into your
synthesis tool:

```bash
crossagent result job_20260718T120000_a1b2c3d4 | \
  sed 's/^/Codex: /' >> /tmp/synthesis.md
```

## If the job is still running

```bash
crossagent status job_20260718T120000_a1b2c3d4 --json
# {"schema_version":1,"job_id":"job_20260718T120000_a1b2c3d4","status":"running",...

crossagent wait job_20260718T120000_a1b2c3d4 --timeout 45 --json
```

Continue polling with bounded waits until terminal.

## If the job failed

```bash
crossagent status job_20260718T120000_a1b2c3d4 --json
# {"schema_version":1,"job_id":"job_20260718T120000_a1b2c3d4","status":"failed","error":"Advisor exited with code 1",...

crossagent logs job_20260718T120000_a1b2c3d4 --stream stderr
```

Inspect the error and decide whether to retry with a tighter context package.

## If the job is cancelled

```bash
crossagent status job_20260718T120000_a1b2c3d4 --json
# {"schema_version":1,"job_id":"job_20260718T120000_a1b2c3d4","status":"cancelled",...

crossagent result job_20260718T120000_a1b2c3d4
# Error: job is cancelled — no result available
```

No result exists. Start fresh if needed.

## Cancellation

If you decide the question is no longer relevant:

```bash
crossagent cancel job_20260718T120000_a1b2c3d4 --wait
```

This creates a `cancel.request` file in the job directory; the worker's next
poll sees it, terminates the advisor gracefully (SIGTERM → process group), waits
the termination grace period (default 10 s), then force-kills (SIGKILL) the
process tree. The final state is `cancelled`. Cancelling an already-terminal job
is a no-op (exits 0).

## Key rules

1. **Never start two jobs for the same decision.** Recover by job ID instead.
2. **Never wait indefinitely in one tool call.** Use bounded `--timeout` waits.
3. **Keep the job ID visible.** It is the only handle to a running or completed
   job.
4. **`running` is not a failure.** The bounded-wait exit code is 0 for `running`
   and all terminal states. Use `--require-complete` if you need a non-zero exit
   for non-terminal results.
5. **Prompt text never appears in `status`, `state.json`, or `command.json`.**
   It is written to the job directory's `prompt` file (mode 0600) and read only
   by the worker.

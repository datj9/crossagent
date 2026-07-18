# Example — Debugging second opinion

**Situation:** You've been staring at a flaky test for an hour. Your current agent
keeps proposing the same fix. Get a fresh, independent diagnosis from a different
agent before you spend more time.

## 1. Package the symptom, not your theory

Give the advisor the evidence and let it form its own hypothesis — don't lead it
to your conclusion.

```bash
cat > /tmp/flaky.md <<'EOF'
<decision>Root cause of the intermittent failure in tests/orders.spec.ts::"cancels a pending order".</decision>
<current_state>
Fails ~1 in 5 local runs, ~1 in 3 in CI. Always the same assertion: order.status
expected "cancelled" but got "pending".
</current_state>
<evidence>
- tests/orders.spec.ts:41
- src/orders/cancel.ts:20  (awaits a DB write then reads back)
- No explicit transaction around the write+read.
</evidence>
<context_collection>
You may run: rg, sed, git log -p on these files. Read-only. Do not run the test suite.
</context_collection>
<questions>
1. Your independent hypothesis for the flakiness?
2. What evidence in the files supports or refutes it?
3. Smallest reliable reproduction or check?
</questions>
<response_format>Position / Evidence / Risks And Counterarguments / Recommendation / Unresolved Questions</response_format>
EOF
```

## 2. Consult a different model than the one that's stuck

```bash
consult --agent claude --name orders-flaky-cancel --cwd "$PWD" \
  --tools "Read,Grep,Bash" --prompt-file /tmp/flaky.md
```

## 3. Compare

If the advisor independently lands on the same root cause you suspected, your
confidence should rise. If it points somewhere else — a race between the write
and the read-back, say — you just saved an hour chasing the wrong fix. Either
way, **you** verify the cited lines before changing code.

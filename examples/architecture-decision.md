# Example — Architecture decision

**Situation:** You're in Codex working on a payments service and you're torn on
where a retry should live. You want Claude to challenge your leaning before you
commit.

## 1. Package the decision

```bash
cat > /tmp/retry.md <<'EOF'
<role>
You are a senior teammate in a joint decision. Evaluate independently, inspect
read-only context if useful, and return an evidence-backed critique. Do not edit files.
</role>
<decision>Should the payment retry live in the async worker or the API request path?</decision>
<current_state>
Retries are inline in the API handler today. p99 latency regressed 40% under load.
Idempotency keys already exist on the charge endpoint.
</current_state>
<constraints>No new infra this quarter. Must remain idempotent. Ship in 1 week.</constraints>
<evidence>
- src/api/payments.ts:88  (inline retry loop)
- worker/queue.ts:12      (existing queue consumer)
- bench/2026-07.md        (load test showing the regression)
</evidence>
<questions>
1. Which layer, and why?
2. Strongest counterargument to your pick?
3. Cheapest way to validate before committing?
</questions>
<response_format>Position / Evidence / Risks And Counterarguments / Recommendation / Unresolved Questions</response_format>
EOF
```

## 2. Ask a peer

```bash
crossagent --agent claude --name payments-retry-design --cwd "$PWD" \
  --tools "Read,Grep" --prompt-file /tmp/retry.md
```

`--tools "Read,Grep"` lets Claude open the cited files to verify claims, but not modify anything.

## 3. Follow up on the same decision

New load numbers came in. Resume — no need to re-explain:

```bash
cat > /tmp/retry-followup.md <<'EOF'
Resuming the same decision. New evidence: moving retries to the worker in a spike
dropped p99 by 35% but added ~2s worst-case settlement delay. Does that change your
recommendation?
EOF

crossagent --name payments-retry-design --prompt-file /tmp/retry-followup.md
```

## 4. Synthesize

Reconcile Claude's critique with your own view: where do you agree, where do you
diverge, and what's the cheapest next check? Make the call — don't just adopt the
last answer.

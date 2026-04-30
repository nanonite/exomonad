# E2E Chainlink Issue Close Test — Root TL Protocol

**OVERRIDE: Automated E2E test. Execute immediately, no planning.**

You are the ROOT TECH LEAD in E2E chainlink close test mode.

## Steps — DO THIS NOW, IN ORDER

1. **Create a team** via `TeamCreate` immediately.

2. **Call `chainlink_issue_create`** with `title`: "E2E chainlink close test", `priority`: "low".
   Note the returned `issue_id`.

3. **Call `spawn_worker`** with:
   - `name`: "close-worker"
   - `task`:
     ```
     Issue ID: <issue_id from step 2>

     You are testing chainlink_issue_close. Do these steps:

     1. Run in bash: chainlink agent init close-worker
     2. Call MCP tool: chainlink_session_work with issue_id=<issue_id>
     3. Write file: chainlink-close-output.txt containing "Worker close test passed"
     4. Call MCP tool: chainlink_issue_close with issue_id=<issue_id> and summary="E2E close test completed"

     Step 4 atomically: release locks → close issue → end session → notify parent TL.
     After step 4, stop.
     ```

4. **STOP. Idle.** The worker will call chainlink_issue_close → notify_parent reaches you.

5. **When you see the worker's notification** (teammate message like "Closed #<id>: ..."):
   - Write `chainlink-close-result.txt` containing `SUCCESS`
   - Call `send_message` with `target_name`: "test-runner", `message`: "[CHAINLINK-CLOSE-DONE] issue #<issue_id> closed"

6. **Stop.**

## NEVER
- Implement code yourself
- Create files/commits outside these steps
- Merge PRs, run gh, or curl the server socket

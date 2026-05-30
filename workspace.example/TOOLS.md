# Tools

## File Operations
- **read**: Read a file from the workspace or allowed paths.
- **write**: Write content to a file.
- **edit**: Make targeted edits to an existing file.

## Communication
- **message**: Send a message to a contact via the configured channel.
- **tts**: Generate and send a voice message.
- **react**: React to a message with an emoji.

## Research
- **web_search**: Search the web using the configured search provider.
- **web_fetch**: Fetch and read a web page.
- **memory_search**: Search long-term memory by keyword or semantic similarity.
- **memory_get**: Retrieve a specific file snippet from memory.

## Memory Management
- **memory_write**: Store a fact (entity-attribute-value) in structured memory.
- **memory_forget**: Invalidate a stored fact.
- **commitment_update**: Update the status of a commitment (open, done, expired, cancelled).

## System
- **exec**: Run a shell command with timeout.
- **session_status**: Check current session stats (cost, tokens, uptime).
- **sessions_spawn**: Spawn a sub-agent for a focused task.
- **load_skill**: Load a skill's instructions on demand.

## System tools (available via exec)

These are installed in the runtime environment. Use them through the **exec** tool when the situation calls for it.

- **at** — Schedule a one-off deferred command. Use for reminders, delayed actions, timed follow-ups.
  `echo "lucydctl --notify 'Reminder: call dentist' --source reminder" | at now + 30 minutes`
- **cron** — Recurring schedules. Edit via `crontab -e`.
- **curl** — HTTP requests. Useful for APIs, webhooks, or fetching data that web_fetch can't handle.
- **jq** — JSON processing. Filter, transform, and query JSON from other commands.
- **git** — Version control. Commit workspace changes, check history, manage branches.
- **ssh/scp** — Remote system access if configured.

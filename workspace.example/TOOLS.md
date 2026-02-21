# Tools

## File Operations
- **read**: Read a file from the workspace or allowed paths.
- **write**: Write content to a file.
- **edit**: Make targeted edits to an existing file.

## Communication
- **message**: Send a message to a contact via the configured channel.
- **tts**: Generate speech audio and optionally send to a contact.
- **react**: React to a message with an emoji.
- **schedule_message**: Schedule a message for future delivery.
- **list_scheduled**: List pending scheduled messages.

## Research
- **web_search**: Search the web using Brave Search.
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

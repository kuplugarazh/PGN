# Changelog

## [0.2.0] - 2026-11-18

### Changed
- Reworked the agent core around a compact toolset.
- Removed legacy workflow state machine and task-state machinery.
- Increased Telegram conversation limit from 3 to 10 turns.
- Updated environment loading to resolve .env relative to bot.py.
- Updated system prompt and project identity to PGN.

### Added
- Bubblewrap-based shell sandboxing.
- Separate privileged system tool with confirmation.
- Recoverable deletion quarantine.
- Automatic backups before file modifications.
- Desktop control and screenshot tools.
- Yandex Music integration.
- Automatic context compaction.

### Removed
- Legacy PAI workspace wrappers.
- Redundant tool implementations.
- Text-based security parsers.

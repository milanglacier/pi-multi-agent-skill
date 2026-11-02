# Changelog

## [0.2.0] - 2026-10-26

### Breaking Changes

- Environment variables use the `PI_MULTI_AGENT_` prefix, such as `PI_MULTI_AGENT_PI_BIN`. The old `MULTI_AGENT_` names are not read.

### Added

- `rm --all` deletes every job of the current project that is not running.
- Starting a job with a name already in use fails and reports the existing job's status and age.
- `run` and `resume` check that the pi program exists before starting a job.
- Agents run `pi-request-user-input` in non-interactive mode, so its tool fails instead of opening a dialog.

## [0.1.0] - 2026-10-12

### Added

- Initial release. The `multi-agent` skill and its CLI run pi agents as named foreground or background jobs that can be waited on, steered, aborted, resumed, and inspected.

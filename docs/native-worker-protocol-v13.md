# Native Worker Protocol v13

The Mac app and bundled worker require protocol 13 and ship together. Quality
mapping, source requests, conversion results, signals, cancellation, cleanup,
and unattended retry policy retain their [v12 behavior](native-worker-protocol-v12.md).
Version 12 requests/events are incompatible; historical v12 fixtures remain
rejection evidence. Shared current fixtures use the `native_worker_*_v13.json`
names in `tests/fixtures`.

## Input and readiness

One reader owns stdin from its first byte. The first UTF-8 JSONL record is the
existing `job.start` JobSpec, with the existing 64 KiB limit. Processing starts
after this record; the host keeps stdin open for controls. Remaining JSONL
records have a separate 4 KiB limit, including LF. Split and coalesced records
are supported. An oversized control is discarded through its LF before framing
resumes, so its suffix cannot become a new command. A final record at EOF is
accepted. Malformed controls are rejected without echoing their contents.

`worker.ready` adds:

```json
{"control_capabilities":["keep_waiting_v1"]}
```

The host enables Keep Waiting only after this capability and a current
`tool.stall` event with `can_extend: true`. The production warning threshold is
60 seconds, half the existing 120-second output watchdog. The notice is
nonmodal; unattended jobs retain the existing timeout and automatic retry.

EOF disables incoming controls without cancelling the job. The reader uses a
duplicated descriptor and readiness waits, so job completion stops it even if
the host keeps input open. It does not close the host's descriptor. In-memory
StringIO provides the finite test equivalent; arbitrary blocking TextIO objects
are unsupported. Child processes receive DEVNULL for otherwise inherited worker
stdin. Explicit media-pipeline stdin and explicit file inputs are preserved.

## Keep Waiting command

```json
{
  "protocol_version":13,
  "type":"job.keep_waiting",
  "command_id":"2335b435-bdf4-47d2-b47b-c6bbcb063349",
  "job_id":"c1ebce77-0460-462b-bd7e-8498c0355267",
  "tool_run_id":"ce342bf4-ac4f-4a6e-a920-3616a9b68693",
  "stall_episode_id":"09329f41-4707-4086-9a61-bc4131e67790"
}
```

These are the exact allowed fields. All four identities are UUID strings. The
worker assigns `tool_run_id` and `stall_episode_id`; the host generates a new
command ID for each deliberate action. The caller cannot choose a duration.
Stop continues to use the existing cancellation signals and ownership path.

The runner applies at most two fixed 120-second grants per episode. Real
artifact progress includes the existing size growth, advancing modification
time, and supported resolver transitions. Logs, heartbeats, control receipt,
and acknowledgement never count as artifact progress. All watched outputs must
be below the warning age before recovery ends an episode and resets its
budget. A growing sibling eye therefore cannot refresh a stalled eye's grants.

Each grant extends the existing deadline by 120 seconds, not by 120 seconds
from when a delayed command is processed. The extension must fit within the
episode's original earliest output deadline plus 240 seconds. Partial output
recovery may advance the normal deadline but cannot remove this hard cap. A
grant is rejected if its full fixed interval would cross the cap.

A command must have arrived before the current deadline. If the runner was
delayed, it may apply that queued grant only while the resulting fixed extension
window remains open. A committed timeout, cancellation, or completed attempt
cannot be revived. Normal no-command timeouts still use each artifact's actual
progress time and the existing timeout duration.

The runner refreshes monotonic time after probing output files and immediately
before each grant. Slow filesystem probes or earlier acknowledgement delivery
cannot reuse an older poll timestamp to bypass the extension window.

## Reliable result event

`control.result` uses the ordinary worker event envelope and sequence. The
envelope's `job_id` is always the actual worker job, even for a rejected command
claiming another job. Its payload is:

| Key | Type | Meaning |
|---|---|---|
| `command_id` | UUID string, optional | Present when a valid identity could be decoded |
| `tool_run_id` | UUID string, optional | Exact submitted attempt |
| `stall_episode_id` | UUID string, optional | Exact submitted stall episode |
| `accepted` | Boolean | True only after the runner applied the grant |
| `code` | String | Decision described below |
| `duplicate` | Boolean | Replayed immutable decision |
| `grants_used` | Integer, optional | Applied grant count at the original decision |
| `grant_seconds` | Integer, optional | 120 for an accepted grant |

```json
{
  "command_id":"2335b435-bdf4-47d2-b47b-c6bbcb063349",
  "tool_run_id":"ce342bf4-ac4f-4a6e-a920-3616a9b68693",
  "stall_episode_id":"09329f41-4707-4086-9a61-bc4131e67790",
  "accepted":true,"code":"extended","duplicate":false,
  "grants_used":1,"grant_seconds":120
}
```

Rejections use `invalid_control`, `control_too_large`, `wrong_job`,
`inactive_run`, `stale_episode`, `not_stalled`, `cancelled`, `run_ended`,
`deadline_elapsed`, `extension_window_elapsed`, `grant_limit`,
`command_conflict`, `queue_full`, `ledger_full`, or `job_ended`.

The job's queue holds at most 16 pending commands. Its decision ledger holds
256 command IDs and does not evict outcomes. IDs enter the ledger at submission,
before runner application. Identical pending duplicates share one eventual
decision; completed duplicates replay that decision with `duplicate: true`
without changing the deadline or count. Reusing an ID for another target is a
conflict. Once the ledger is full, new IDs remain rejected for that job.

Acknowledgements use the worker event transport directly, not the best-effort
diagnostic or observability queue. No remaining-time countdown is included: an
old grant outcome cannot become a new apparent deadline. The UI clears pending
actions on recovery, terminal events, retry, or transport failure and never
redirects an old action to a new attempt.

## Stall and diagnostic event

`tool.stall` also uses the ordinary worker envelope. Its payload has:

| Key | Type | Meaning |
|---|---|---|
| `tool_run_id`, `stall_episode_id` | UUID string | Worker-owned attempt and episode |
| `tool` | String | Existing public tool ID |
| `state` | String | `stalled`, `extended`, `recovered`, `timed_out`, or `ended` |
| `can_extend` | Boolean | Current capability for this episode |
| `grants_used`, `max_grants`, `grant_seconds` | Integer | Used count, 2, and 120 |
| `artifacts` | Array | At most 16 artifact summaries |
| `artifacts_omitted` | Integer | Additional watched outputs, if any |
| `tool_progress` | Object | Bounded numeric progress/diagnostic summary |

Each artifact includes `role` (existing public role ID), `state` (`missing`,
`stalled`, or `growing`), and integer `no_progress_age_seconds`. Optional integer
`size_bytes` is the last observed file size. No path, filename, raw command,
media title, or diagnostic line is copied into this event.

`tool_progress` always includes integer `updates`, `repeated_updates`,
`last_output_age_seconds`, and `output_bytes`. Optional integer
`updated_age_seconds` and `changed_age_seconds` distinguish a repeating parsed
progress value from one that changed. Optional finite numbers `fraction`,
`completed_units`, and `total_units` describe the latest parsed progress. They
remain diagnostic evidence; they cannot renew output-watchdog deadlines. The
existing stage-level `progress` field and its schema are unchanged.

Events are emitted when a stall is noticed, a grant is applied, all outputs
recover, the watchdog expires, or an active stalled run ends. Successful
finalization detected by process exit does not create a new recovery offer.
The direct route watches the final MV-HEVC encoder's output; the generated route
watches all outputs on its final ffmpeg stage.

This protocol does not repair media or remux an MKV. A remux recovery route
requires a reproducing source and validation that MVC/3D video, audio, and
subtitles survive and that the resulting source converts successfully.

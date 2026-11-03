---
name: pg-replication-lag
description: >-
  Replication facts for a PostgreSQL instance, primary or standby. On a
  primary, each connected replica (state, sync state, write, flush and replay
  lag in ms, LSN distance in bytes) and each replication slot (active,
  wal_status, WAL retained, safe_wal_size). On a standby, the WAL receiver
  (status, sender), received against replayed LSN, replay delay in ms, and
  whether replay is paused. Use when a replica is behind, reads are stale,
  failover is in doubt, or the primary's disk is filling because of WAL a slot
  retains.
runwhen:
  role: health
  appliesTo:
    types: [postgres/instance]
  tags: [health, replication, wal, slots]
---

## When to use

A replica is serving stale reads, a failover is being planned, replication alerts fire, or the
primary's WAL volume grows. Run `replication-status` against the primary to see its replicas and
slots, and against the standby to see how far it has received and replayed. The task finds out
which one it is on (`pg_is_in_recovery()`) and returns the matching part. It only reads, in a
read-only session.

## What each task tells you

`replication-status` returns `replication` with `role` (`primary` or `standby`) and `visibility`.

- On a primary, `primary` has `currentLsn`, `replicas` and `slots`.
  - `replicas` (pg_stat_replication, one row per walsender): `applicationName`, `clientAddr`, `state`, `syncState`;
    `writeLagMs`, `flushLagMs`, `replayLagMs` (the interval columns, in milliseconds); and
    `sentLsnBytes`, `writeLsnBytes`, `flushLsnBytes`, `replayLsnBytes`, the distance of each
    position from `pg_current_wal_lsn()`, which is read once and returned as `currentLsn`, so
    every distance is from that one position.
  - `slots` (pg_replication_slots): `slotName`, `slotType`, `active`, `walStatus`,
    `retainedWalBytes` (`pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)`) and `safeWalSize`.
- On a standby, `standby` has `receiver` (`status`, `senderHost`, `senderPort`, `slotName`,
  `lastMsgReceiptAgeMs`, `latestEndLsn`), `receiveLsn`, `replayLsn`, `receivedNotReplayedBytes`,
  `receiveEqualsReplay`, `replayDelayMs` and `replayPaused` with `replayPauseState`.

## How to read the result

Outputs are facts, not verdicts.

- A primary with `replicas.total: 0` has no replica connected right now; with `slots.total: 0`
  it has no slot. Both are facts about the instance, not errors. Compare with what should exist.
  `replicas` is every walsender, not only standbys: `pg_basebackup`, `pg_receivewal` and
  logical-replication subscribers appear there too (see `applicationName` and `usename`).
- An inactive slot (`active: false`) with a large `retainedWalBytes` makes the primary keep all
  WAL since the slot's `restart_lsn`, and that fills the disk. `walStatus: lost` means the
  WAL the slot needed has already been removed: its `retainedWalBytes` is then only the distance
  from `restart_lsn` to the current position, not WAL that is still on disk, and `safeWalSize` is
  null. For other slots `safeWalSize` is how much more WAL may be written before that happens
  (null means no `max_slot_wal_keep_size` limit).
- `writeLagMs`, `flushLagMs`, `replayLagMs` are null when the standby is caught up and idle: the
  primary has no recent measurement, which is not zero. Read the byte distances beside them;
  all 0 means nothing is outstanding.
- `replayDelayMs` on a standby is now minus the commit time of the last replayed transaction
  (`pg_last_xact_replay_timestamp()`). It grows while the primary is idle, so a quiet primary
  makes a perfectly current standby look minutes behind. That reading applies only while
  `receiver.status` is `streaming` and `receiver.lastMsgReceiptAgeMs` is recent (an idle primary
  still sends keepalives, so a connected receiver hears something well inside
  `wal_receiver_timeout`, 60s by default). Then `receiveEqualsReplay: true` means the standby has
  replayed everything it received, so the delay is the primary's silence, not a backlog; and when
  `receivedNotReplayedBytes` is 0 and the standby's `receiveLsn` matches the primary's
  `currentLsn`, it is current. With no receiver (`receiver: null`), a `status` other than
  `streaming` or a stale last message, the standby is cut off from the primary: it can still
  say `receiveEqualsReplay: true` because it replayed all it ever received, and it falls further
  behind with every write on the primary. A standby whose `receiveLsn` is below the primary's
  `currentLsn` is not receiving everything. `latestEndLsn` is the last position the sender
  reported. `replayDelayMs` is null until a transaction has been replayed.
- `receiveEqualsReplay: false` with a `receivedNotReplayedBytes` that stays above 0 is replay
  falling behind (long queries conflicting with replay, I/O, or replay paused).
- `replayPaused: true` (`replayPauseState` `paused`; `pause requested` is on its way) means
  replay was stopped on purpose, with `pg_wal_replay_pause()`; WAL keeps arriving and the
  standby falls behind until it is resumed.
- `receiver: null` on a standby means no WAL receiver is running: it recovers from archive or
  is disconnected. `receiveLsn` does NOT go null with it: it keeps the last position the receiver
  flushed after the receiver has exited, so it shows how far the standby got, not that it is
  connected. It is null only when WAL was never received (pure archive recovery).
- `visibility: partial` (with a `permission_denied` error scoped to `pg_stat_replication` or
  `pg_stat_wal_receiver`): the role is not a member of `pg_monitor` (or `pg_read_all_stats`). The
  views show their rows but hide state, lag and sender, so those fields are null; an empty or
  null view then proves nothing. Slots, LSN positions and pause state are not hidden. Grant the
  role and run again.
- A standby's own downstream replicas and slots are not listed. Replicas and slots are cut to
  `maxItems` (default 200) in the query; `total` is the count before the cut and `truncated` says
  whether any were left out.
- Needs PostgreSQL 13 or later (below it: `dependency`, "PostgreSQL 13 or later required"). The
  pause state (`replayPauseState`) needs 14: on 13 it is null and `replayPaused` is still read.
  Otherwise the run fails with a code, not an empty result: `permission_denied`, `not_found`,
  `unreachable`, `timeout`, `dependency` (no psql).

## What to try next

- Replica missing from a primary that should have one: run this task on the standby and check
  `receiver` and the sender host; then the standby's pod and volume with
  `kubernetes:k8s-workload-health/rollout-status`.
- Inactive slot retaining WAL: find its consumer; if it is abandoned, drop it
  (`pg_drop_replication_slot`, a change this skill does not make) and watch the disk.
- Replay behind with `receiveEqualsReplay: false`: long queries on the standby (the postgres
  sessions skill) or paused replay (`pg_wal_replay_resume()`).
- Lock waits on the primary: `postgres:pg-locks/blocking`.

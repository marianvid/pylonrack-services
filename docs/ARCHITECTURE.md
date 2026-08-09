# How the slot is built

Written for whoever opens this in six months — including its author.

---

## The shape

```
PylonRack (Swift)                        the Mac
   │                                        │
   ├─ ws://127.0.0.1:8766 ──── server.py ───┤  rack protocol: header, status
   │                              │         │
   └─ WebView ─ http://:8768 ─ uiserver.py ─┤  the body panel and its live feed
                                  │         │
                          InstanceManager   │
                                  │         │
                       ┌──────────┼─────────┴──┐
                  llama-server  llama-server  llama-server
                     :8771         :8772         :8773
```

Two servers, one process. The rack channel carries the header controls the rack
draws natively; the HTTP port carries the panel below them, which the slot
renders itself.

### Why the body is HTML and not a native view

PylonRack renders a slot body either as one of its own Swift views — log,
models, settings — or as a WebView pointed at `ui_url`. A table of N instances
is none of those, and adding one would mean changing and rebuilding the rack
app for every slot that wants a different layout.

Serving the panel from the slot keeps the two independent: this repository can
change its entire interface without the rack knowing.

---

## The files

| File | Holds |
|---|---|
| `config.py` | `settings.json` ↔ dataclasses, atomic writes, port allocation |
| `instances.py` | process lifecycle, state derivation, the memory guard, metrics |
| `uiserver.py` | HTTP for `ui/`, WebSocket for the live feed |
| `server.py` | rack protocol, and the command layer both callers share |
| `ui/index.html` | the panel: instance table, log view, dialogs |
| `model_scanner.py`, `parent_watchdog.py` | copied verbatim from `pylonrack-llama` |

The duplication of those last two is deliberate. A shared package across
separate repositories has to be versioned, released and kept in step; at forty
lines each, copying costs less than coordinating. If either grows, revisit.

---

## Decisions worth knowing

### State is derived, never remembered

`ManagedInstance.state` recomputes on every read: is the process alive, does the
model file still exist. Nothing writes "running" into a variable and trusts it
later.

A remembered state drifts. The process can die on its own and the row keeps
claiming Running; a model file can be deleted while an instance sits idle and
nobody notices until a start fails with something cryptic. Deriving costs a
`poll()` and a `stat()` — microseconds — and buys a table that cannot lie.

### Missing is not Error

A model file that vanished gets its own state. An absence is not a fault: the
configuration is still valid, the instance simply has nothing to load. So the
row stays, marked, with the path and a way to point it somewhere else — and if
the file comes back the instance recovers on its own.

Deleting the configuration instead would be the easy path and the wrong one. A
deleted instance is a forgotten one.

### The memory guard runs before the spawn

`can_start()` refuses when the total would pass *installed RAM − 8 GB*. It sums
**model file sizes**, not resident memory: RSS lags during load, and the
question has to be answered before the memory is actually taken.

It therefore counts weights only. The KV cache grows with context and parallel
slots and sits on top — at a large context that is significant. The figure is
useful, not exact.

There is no override. The failure it prevents is a machine that stops
responding, which is not a state anyone can click their way out of.

### The event loop does no I/O

`snapshot()` is built on the loop several times a second, so it must stay cheap.
Resident memory and in-flight request counts are measured by a poller thread
every three seconds; the snapshot reads the cached numbers. Everything else that
can block — starting, stopping, scanning the cache, shutting down — goes through
`run_in_executor`.

A test asserts that `snapshot()` never touches the network. Without it the rule
is a comment, and comments do not fail.

### One command layer, two callers

The rack header and the body panel call the same functions in `Commands`. They
cannot disagree about what `start` means, and a rule added in one place applies
in both.

### Configuration is written atomically

`config.save()` writes a temporary file, fsyncs it and renames. A crash midway
leaves the previous file intact. Truncating and rewriting in place would lose
every instance definition if the power went at the wrong microsecond.

Instance state (`was_running`) is saved with it, so a restart brings back what
was up — in the background, after the listener opens, since loading a large
model takes minutes and the rack should not be kept waiting on it.

---

## The rack protocol, as used here

Incoming, from the rack:

| Message | Response |
|---|---|
| `manifest` | the manifest, then a `controls_update` |
| `ping` | `pong` with status and a one-line summary |
| `action` with `control_id` | runs `start_all` or `stop_all` |
| `log_request` | `log_response` with the merged tail |
| `shutdown` | saves, then stops everything off the loop |

Outgoing, unprompted: `controls_update` whenever an instance changes state.

The manifest declares `modes: ["instances"]` and a `ui_url`. The log is not a
declared mode: the rack's log panel shows one undivided stream, and with several
servers writing at once the only useful log is one that can be filtered — so it
lives in the body, with a chip per instance.

## The panel protocol

The page opens a WebSocket to `/ws` on the UI port and sends
`{action, payload, req}`. Every action returns `{ok, ...}` tagged with the same
`req`. Unprompted `snapshot` pushes arrive every two seconds.

Actions: `snapshot`, `start`, `stop`, `start_all`, `stop_all`,
`available_models`, `add`, `update`, `remove`, `relocate`, `log`.

The snapshot row carries the **whole** instance record, not just the columns the
table draws. The edit dialog is built from it; when the row carried only the
visible subset the rest arrived undefined, dropdowns fell back to their first
option, and saving wrote back values nobody had chosen. A test asserts every
field of `Instance` appears in the row.

---

## Ports

| Port | What | Set in |
|---|---|---|
| 8766 | rack channel | `rack.json` |
| 8768 | UI server | `settings.json` |
| 8771–8829 | one per instance | allocated, first free |

`8765` belongs to `pylonrack-llama`, `8767` to `pylonrack-calibrate`, `8769` to
the ParallaxVox slot. All can be loaded at once.

Allocation raises when the range is full rather than reusing a port, and
`can_start()` probes the port before spawning. Two servers on one port means the
second dies with an error nobody reads.

---

## Testing

43 tests. Nothing loads a model, nothing reaches the network, nothing depends on
what happens to be running on the machine.

That last point was learned the hard way: `can_start()` probes whether a port is
free, so a test using 8771 passed or failed depending on whether an instance
happened to be up outside the test run. A suite that answers differently on
unchanged code is worse than no suite. The probe is stubbed by an autouse
fixture and covered by one test of its own.

The tests worth reading first, because they encode decisions rather than
behaviour:

- `test_snapshot_carries_every_editable_field` — the dialog contract
- `test_metrics_are_read_from_cache_not_the_network` — the loop stays free
- `test_busy_port_blocks_start` — the probe the other tests switch off
- `test_next_free_port_raises_when_full` — no silent reuse

Run them with `.venv/bin/python3 -m pytest tests/ -q`.

---

## What is deliberately absent

**Model downloading and Hugging Face browsing.** `pylonrack-llama` has both. Two
implementations means two places to fix the same thing.

**Rebuilding llama.cpp.** Same reason.

**Start at login.** PylonRack has its own setting. Two switches for one
behaviour can only disagree.

**An override for the memory guard.** See above.

---

## Where it could go wrong

Honest list, for whoever debugs this next.

**The KV cache is invisible to the guard.** Several instances at a large context
can pass the check and still exhaust memory. Watch the measured column, not the
committed one.

**`llama-server` has no authentication of its own.** Bound to `0.0.0.0`, anyone
on the network can use it. Set an API key per instance, or bind to `127.0.0.1`.

**Instances outlive the slot.** They are spawned with `start_new_session`, so a
rack crash leaves them running. They are cleaned up on the next orderly
shutdown, or by hand.

**A model swapped in place.** If a file is replaced with a different model at
the same path, nothing notices — size and path are unchanged.

**`lan_ip()` is resolved once.** Changing network while the slot runs shows a
stale address until it restarts.

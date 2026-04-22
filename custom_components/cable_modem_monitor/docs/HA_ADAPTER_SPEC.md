# HA Adapter Specification

How the Home Assistant integration wires Core and Catalog into HA's
lifecycle. This spec defines the adapter layer — everything between
Core's synchronous API and HA's async event loop.

**Design principle:** The adapter is thin. Core owns all modem logic
(auth, parsing, polling policy, restart recovery). The adapter owns
scheduling, entity creation, and HA lifecycle. If logic could live in
Core, it should.

**Minimum HA version:** 2024.12 (for `entry.runtime_data` support).

**Related specs:**

- `ENTITY_MODEL_SPEC.md` — what entities to create
- `CONFIG_FLOW_SPEC.md` — setup wizard and options flow
- `ORCHESTRATION_SPEC.md` — Core's interface contracts
- `RUNTIME_POLLING_SPEC.md` — polling behavior and signal policy
- `ARCHITECTURE.md` — system design and package boundaries

---

## Contents

| Section | What it covers |
|---------|----------------|
| [Runtime Data](#runtime-data) | `CableModemRuntimeData` structure on `entry.runtime_data` |
| [Startup](#startup) | `async_setup_entry` — component creation and wiring |
| [Unload](#unload) | `async_unload_entry` — cleanup and cancellation |
| [Async Boundary](#async-boundary) | Which Core calls need executor wrapping |
| [Data Coordinator](#data-coordinator) | DataUpdateCoordinator wrapping `get_modem_data()` and deferred entity creation |
| [Health Coordinator](#health-coordinator) | Second coordinator wrapping `health_monitor.ping()` |
| [Polling Modes](#polling-modes) | Scheduled, disabled, manual trigger |
| [Restart Lifecycle](#restart-lifecycle) | Button → executor → one-shot command → return |
| [Recovery Adapter](#recovery-adapter) | Observer + cadence listener that reacts to Core's `recovery_active` flag |
| [Operation Mutex](#operation-mutex) | `active_operation` field — mutex between destructive buttons (restart, reset) |
| [Reset Entities Concurrency Guard](#reset-entities-concurrency-guard) | `active_operation` guard, `_attr_available` toggle, null-safety |
| [Reauth Flow](#reauth-flow) | Circuit breaker → `async_step_reauth` |
| [Diagnostics Platform](#diagnostics-platform) | Core diagnostics + HA-side data |
| [Services](#services) | `generate_dashboard`, `request_refresh`, `request_health_check` |
| [Config Entry Migration](#config-entry-migration) | Version-keyed migration with auto-discovery |
| [Testing](#testing) | No modem-specific names, dynamic catalog discovery |
| [Distribution](#distribution) | HACS zip, PyPI packages, version pinning, release tiers |

---

## Runtime Data

All runtime state lives on `entry.runtime_data`. HA manages cleanup
automatically on unload.

```python
# ModemIdentity is defined in Core (see ORCHESTRATION_SPEC.md § Data Models).
# Populated from modem.yaml at config load time. Fields: manufacturer,
# model, docsis_version, release_date, status.


@dataclass
class CableModemRuntimeData:
    """All runtime state for one config entry."""

    data_coordinator: DataUpdateCoordinator
    health_coordinator: DataUpdateCoordinator | None
    orchestrator: Orchestrator
    health_monitor: HealthMonitor | None
    modem_identity: ModemIdentity
    active_operation: Literal["restart", "reset"] | None = None


type CableModemConfigEntry = ConfigEntry[CableModemRuntimeData]
```

**`active_operation`** is the mutex for destructive buttons —
Restart and Reset Entities. Set to `"restart"` or `"reset"` while
the corresponding button handler runs, cleared in a context
manager's `finally`. A second destructive press while one is
running is refused. See § Operation Mutex and § Reset Entities
Concurrency Guard.

It is adapter-layer state, separate from Core's `recovery_active`
flag. The two answer different questions:

- `active_operation` — is a destructive *button handler* currently
  running? (True for ~2–5 s during a restart button press, or for
  seconds-to-minutes during a reset.)
- `orchestrator.recovery_active` — is the *modem* currently in a
  recovery window? (True for the duration of
  `_RECOVERY_WINDOW_SECONDS` after any recovery trigger.)

The button is disabled when *either* is set. During a button-press
restart both are True briefly; after `restart()` returns,
`active_operation` clears while `recovery_active` continues for
the rest of the window.

**Access pattern:**

```python
# In sensor.py, button.py, diagnostics.py
async def async_setup_entry(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data_coordinator
    orchestrator = entry.runtime_data.orchestrator
```

**Why not `hass.data[DOMAIN]`:** The old pattern requires manual dict
management (setdefault, pop) and has no type safety. `runtime_data` is
typed, auto-cleaned, and scoped to the entry.

---

## Startup

`async_setup_entry` loads configs and delegates component assembly to
the Core factory. Config loading stays in the adapter (catalog path
is HA-specific); assembly logic lives in Core.

```text
async_setup_entry(hass, entry)
 │
 ├─ 1. Load configs from catalog
 │     catalog_path / manufacturer / model / modem[-variant].yaml
 │     → modem_config, parser_config, post_processor
 │     (runs in executor — file I/O)
 │
 ├─ 1a. Inject credential encoding (Core concern)
 │      apply_credential_encoding(modem_config, ...)
 │
 ├─ 2. Resolve health probe defaults
 │     modem.yaml health config → defaults
 │     config entry data → overrides
 │
 ├─ 3. Create orchestration graph via Core factory
 │     create_orchestrator(modem_config, parser_config,
 │         post_processor, base_url, username, password, ...)
 │     → (orchestrator, health_monitor, modem_identity)
 │
 ├─ 4. Create data DataUpdateCoordinator
 │     update_method wraps orchestrator.get_modem_data()
 │     update_interval from config (or None if disabled)
 │
 ├─ 4a. Attach the recovery cadence listener (see § Recovery Adapter)
 │      attach_recovery_cadence_listener(hass, entry, orchestrator,
 │                                        data_coordinator)
 │      Registers the observer on Core, installs the cadence listener,
 │      and registers an unsubscribe callback for entry unload.
 │
 ├─ 5. Create health DataUpdateCoordinator (if health_monitor)
 │     update_method wraps health_monitor.ping()
 │     update_interval from config (or None if disabled)
 │
 ├─ 5a. Attach health recovery listener (if health_monitor)
 │      On health RESPONSIVE after non-responsive, triggers
 │      immediate data poll via coordinator.async_request_refresh()
 │
 ├─ 6. Run first poll
 │     coordinator.async_config_entry_first_refresh()
 │     (always runs, even if polling is disabled)
 │
 ├─ 7. Store RuntimeData on entry
 │     entry.runtime_data = CableModemRuntimeData(...)
 │
 ├─ 8. Forward platform setup (sensor, button)
 │
 ├─ 9. Update device registry
 │
 └─ 10. Register services (if first entry)
         generate_dashboard
```

**Steps 1-3 involve sync I/O** — all must run in executor via
`hass.async_add_executor_job()`. Step 3 delegates to the Core
factory which creates the collector, health monitor, orchestrator,
and identity internally.

**Step 6 always runs.** Even when polling is disabled, the first poll
runs during setup so entities have real data. "Disabled" means no
scheduled polls after setup, not "never poll."

---

## Unload

`async_unload_entry` stops scheduled activity and cleans up.

```text
async_unload_entry(hass, entry)
 │
 ├─ 1. Unload platforms (sensor, button)
 │     hass.config_entries.async_unload_platforms(entry, PLATFORMS)
 │     (stops the data + health coordinators' scheduled polls)
 │
 ├─ 2. Unregister services if last entry
 │
 └─ 3. runtime_data auto-cleaned by HA
```

**No restart cancellation primitive.** `orchestrator.restart()` is
one-shot and returns in a few seconds, so there's nothing long-
running to cancel. An in-flight `restart()` executor call (if any)
completes naturally — the worst case is a single ~5 s delay during
unload.

**No threads to join.** Core doesn't spawn threads — HA manages all
scheduling via coordinators and `async_add_executor_job`. Executor
tasks return to the pool when they complete.

**Recovery state survives unload.** `orchestrator.recovery_active`
is memory on the orchestrator instance; when the entry unloads, the
instance is garbage-collected and the state goes with it. Fresh
`async_setup_entry` always starts with `recovery_active == False`.

---

## Async Boundary

Core's API is synchronous (`requests`-based I/O). Every Core call from
HA must go through `hass.async_add_executor_job()`.

| Call site | Core method | Typical duration |
|-----------|------------|-----------------|
| Data coordinator poll | `orchestrator.get_modem_data()` | 2-10s |
| Health coordinator poll | `health_monitor.ping()` | 1-5s |
| Restart button | `orchestrator.restart()` | 2-5s (one-shot) |
| Config flow validation | `list_modems()`, config loading, validation poll | <5s |
| Diagnostics | `orchestrator.diagnostics()` | <1ms (reads memory state) |

**All Core calls are bounded.** `restart()` is one-shot (auth +
POST + session clear); `get_modem_data()` is one poll; `ping()` is
one probe. None of them block the executor thread beyond their own
direct work. Recovery observation — the "keep polling until the
modem is back" behavior — lives on HA's side as a coordinator-
cadence switch (see § Recovery Adapter). Core never waits on
recovery.

---

## Data Coordinator

Wraps `orchestrator.get_modem_data()` in HA's `DataUpdateCoordinator`.

```python
async def _async_update_data() -> ModemSnapshot:
    return await hass.async_add_executor_job(
        orchestrator.get_modem_data
    )

data_coordinator = DataUpdateCoordinator(
    hass,
    logger,
    name=f"Cable Modem {coordinator_label}",
    update_method=_async_update_data,
    update_interval=timedelta(seconds=scan_interval),  # or None
    config_entry=entry,
)
```

Where `coordinator_label` is `"{model} ({host})"` when model is known,
else just `"{host}"`. This ensures HA's built-in coordinator logging
includes both identifiers for log correlation with Core's `[MODEL]`
convention.

**Return type:** `ModemSnapshot` — contains `connection_status`,
`docsis_status`, `modem_data`, `health_info`, `error`. Channel counts
and aggregate fields (e.g., `total_corrected`) are already in
`modem_data.system_info` — computed by the parser coordinator.
Sensors read directly from the snapshot.

**No exception wrapping.** The orchestrator never raises — all failures
are captured in `ModemSnapshot.connection_status` and
`ModemSnapshot.error`. The coordinator always succeeds, and sensors
derive availability from the snapshot content (see
ENTITY_MODEL_SPEC § Availability). The `_async_update_data` wrapper
logs an INFO line (`"Update [MODEL] — no data (status)"`) on failed
polls so the HA-layer log accurately reflects poll outcome alongside
the coordinator's generic `success: True`.

**First refresh:** `async_config_entry_first_refresh()` runs during
setup. Because the orchestrator never raises, this call always
succeeds — even when the modem is unreachable. A failed first poll
returns `ModemSnapshot(UNREACHABLE, modem_data=None)`. The sensor
platform handles this via deferred entity creation (see below).

### Deferred Entity Creation

When the first poll returns `modem_data=None` (modem unreachable at HA
startup), data-dependent entities (channels, system metrics, LAN stats)
cannot be created because they require channel IDs and field presence
from the poll data. The sensor platform handles this by:

1. Creating always-available entities immediately (Status, Info, Health)
2. Registering a one-shot coordinator listener on the data coordinator
3. On each coordinator update, the listener checks for `modem_data`
4. On the first update with `modem_data is not None`: creates
   data-dependent entities via `async_add_entities` and unsubscribes
5. `entry.async_on_unload(unsub)` ensures clean teardown if the entry
   is unloaded before the modem comes online
6. Schedules a delayed re-notification task (1 second) that calls
   `async_set_updated_data(coordinator.data)`, ensuring deferred
   entities receive `_handle_coordinator_update()` after their
   coordinator listeners are registered

This guarantees that:

- Status and health sensors are always visible during outages
- Data sensors appear as soon as the modem becomes reachable
- Deferred entities populate state within 1 second — no Unknown window
  until next scheduled poll
- No duplicate entities — the listener is one-shot
- No leaked listeners — cleanup is automatic

See ORCHESTRATION_USE_CASES.md UC-84 for the full scenario.

---

## Health Coordinator

Second `DataUpdateCoordinator` wrapping `health_monitor.ping()`.
Independent cadence from the data coordinator.

```python
async def _async_update_health() -> HealthInfo:
    return await hass.async_add_executor_job(
        health_monitor.ping
    )

health_coordinator = DataUpdateCoordinator(
    hass,
    logger,
    name=f"Cable Modem {coordinator_label} Health",
    update_method=_async_update_health,
    update_interval=timedelta(seconds=health_check_interval),  # or None
    config_entry=entry,
)
```

**Conditional creation:** Only created if at least one probe works
(discovered during config flow Step 4). When no probes work,
`health_monitor` and `health_coordinator` are None on RuntimeData.

**Independence:** Health probes run on their own timer (default 30s).
The orchestrator reads `health_monitor.latest` during
`get_modem_data()` — no coupling between the two coordinators.
Health sensors update between data polls, giving faster outage
detection.

**Decoupled operation:** Health checks and data collection run
independently — neither suppresses the other. The health monitor
always runs its own probes on its own cadence.

---

## Polling Modes

Data and health intervals are independently configurable. Each can be
scheduled or disabled.

| Data polling | Health check | Coordinator setup |
|-------------|-------------|-------------------|
| Scheduled (default 600s) | Scheduled (default 30s) | Both coordinators with timers |
| Scheduled | Disabled | Data coordinator only, health_coordinator=None |
| Disabled | Scheduled | Data coordinator (no timer), health coordinator with timer |
| Disabled | Disabled | Both coordinators (no timers) |

**"Disabled" means `update_interval=None`** on the DataUpdateCoordinator.
The coordinator still exists (for manual refresh and first poll) but
does not schedule automatic updates.

**Manual trigger:** The "Update Modem Data" button calls
`data_coordinator.async_request_refresh()`. This works regardless of
polling mode — HA's built-in throttling prevents spam.

**Interval limits:**

| Setting | Min | Max | Default |
|---------|-----|-----|---------|
| Data poll interval | 30s | 86400s (24h) | 600s (10m) |
| Health check interval | 10s | 86400s (24h) | 30s |

Configurable via the options flow. Setting to 0 or "Disabled" sets
`update_interval=None`.

---

## Restart Lifecycle

The restart button runs `orchestrator.restart()` on an executor
thread. The Core call is one-shot — authenticate, dispatch the
command, clear the session, trigger the recovery module, return.
Typical duration: 2–5 seconds. Post-reboot polling is handled by
the recovery adapter's cadence switch (see § Recovery Adapter);
the button itself does not observe the reboot.

```text
User presses "Restart Modem"
 │
 ├─ 1. Acquire active_operation = "restart" via context manager
 │     (refuses if another destructive operation is already running).
 │     No gate on recovery_active — a user who sees a flakey modem
 │     after a restart is allowed to try again.
 │
 ├─ 2. Run in executor:
 │     orchestrator.restart()
 │     (returns in 2–5 s; triggers a recovery window internally)
 │
 ├─ 3. Send persistent notification:
 │     success → "Restart command sent"
 │     failure → "Restart command failed: <error>"
 │
 └─ 4. Context manager exits → active_operation cleared. Button is
       immediately available again; the user may press it once more
       if the dashboard shows a flakey state they want to retry.
```

The restart button returns its "busy" state after step 4. Scheduled
data polls run at recovery cadence (driven by § Recovery Adapter)
and surface actual modem state — UNREACHABLE while the modem is
down, transitional docsis states while it ranges, ONLINE once it
returns. The dashboard reflects truth throughout; no synthetic
label.

### Sensor Behavior During a Recovery Window

A recovery window is open whenever `orchestrator.recovery_active`
is True, regardless of what triggered it (commanded restart,
observed outage, heuristic).

| Entity category | Behavior |
|-----------------|----------|
| Status sensor | Renders the snapshot's actual status — Operational / Unreachable / Denied / Not Locked / Auth Failed / etc. No synthetic "Restarting…" label. Always available. |
| Health sensors (ICMP, HTTP, health_status) | Independent coordinator. Continue updating on their own cadence — probes naturally report UNRESPONSIVE while the modem is down and recover when the modem does. |
| Data sensors (channel counts, SNR, power, uptime, system_info fields) | Available when the snapshot's `modem_data` is not None. Unavailable when `modem_data is None` (poll failed — typically UNREACHABLE during the reboot itself). This is the same rule as any non-recovery period; no recovery-specific special case. |
| Per-channel sensors | Same as data sensors. |
| Restart button | Disabled only while `active_operation == "restart"` (during the ~2–5 s command dispatch). After that it's clickable again — the user may choose to retry after observing the dashboard. |
| Update Modem Data button | Press is refused when `active_operation` is set. Normal polling at recovery cadence already refreshes the dashboard; extra manual refreshes would be wasted. |
| Reset Entities button | Press is refused when `active_operation` is set. |

Data sensors going Unavailable on poll failure is the honest
reading: the measurement didn't happen. A gap in time-series
history during a reboot is accurate — uptime and channel power
aren't valid while the modem is off. Holding the last reading
would publish false values.

**Unload during a window:** `async_unload_entry` stops the data
coordinator's scheduled polls. The recovery window state lives on
the orchestrator; it continues to tick but has no observable effect
until the next `async_setup_entry`. All state is memory-only — a
fresh setup starts with `recovery_active == False`.

**HA restart during a window:** executor threads die with the
process. The fresh orchestrator on next startup is in a clean state.

See ORCHESTRATION_USE_CASES.md UC-40 through UC-46, UC-49, UC-72,
UC-78, UC-88 for detailed scenarios.

---

## Recovery Adapter

All HA-side recovery wiring lives in `custom_components/cable_modem_monitor/recovery_adapter.py`.
The module owns the cadence constant, the per-entry dispatcher
signal name, and the single setup entry point that installs the
Core observer and the event-loop listener. Other HA modules don't
reference recovery state directly — they read
`orchestrator.recovery_active` or render snapshot truth.

`__init__.py` imports `attach_recovery_cadence_listener` directly
and calls it once during `async_setup_entry`. `coordinator.py`
stays a pure types module (`CableModemRuntimeData` +
`CableModemConfigEntry`); health-recovery wiring is a small private
helper in `__init__.py` because it's local to startup and
conceptually separate from Core's recovery observer.

### Public surface

```python
# recovery_adapter.py

_RECOVERY_POLL_INTERVAL = timedelta(seconds=30)
# Data coordinator cadence while Core's recovery window is open.


def recovery_state_signal(entry_id: str) -> str:
    """Per-entry dispatcher signal name for recovery transitions."""


def attach_recovery_cadence_listener(
    hass: HomeAssistant,
    entry: CableModemConfigEntry,
    orchestrator: Orchestrator,
    data_coordinator: DataUpdateCoordinator[ModemSnapshot],
) -> None:
    """Install the recovery observer and cadence listener.

    Called once during ``async_setup_entry`` (Step 6a). Registers
    an unsubscribe callback on ``entry.async_on_unload``.
    """
```

### Behavior

- On `attach_recovery_cadence_listener()`:
  - Captures `data_coordinator.update_interval` as the "normal"
    cadence (closure local; NOT stored on RuntimeData).
  - Calls `orchestrator.set_recovery_observer(...)` with a
    thread-safe dispatcher send (hops to the event loop via
    `call_soon_threadsafe`).
  - Connects an event-loop listener on
    `recovery_state_signal(entry.entry_id)` that applies the
    cadence switch.
- On `recovery_active` True→False or False→True:
  - Core fires the observer from the poll thread.
  - The dispatcher send hops to the event loop.
  - The listener reads `orchestrator.recovery_active` and switches
    `data_coordinator.update_interval` between
    `_RECOVERY_POLL_INTERVAL` (True) and the captured normal
    cadence (False).
  - On True, also calls `async_request_refresh()` so the first
    fast-cadence poll happens immediately.
- When the captured normal cadence is `None` (user disabled
  polling): the listener is a no-op — we don't override the user's
  explicit opt-out.

### Why in HA and not Core

Core is synchronous and owns no timers. Pushing the "poll faster"
loop to HA's native scheduling keeps Core free of threads and
bounded-latency concerns and gives timer cancellation, reschedule-
on-interval-change, and event-loop safety for free.

### Consumers

Only `__init__.py` imports from `recovery_adapter.py` —
`async_setup_entry` calls `attach_recovery_cadence_listener()`
once during startup. Sensors and buttons do NOT reference the
recovery signal:

- `sensor.py` — reads snapshot state, which already updates via
  the coordinator on every poll (faster during a window, normal
  outside). No recovery signal subscription needed.
- `button.py` — gates only on `active_operation`. Does not read
  `recovery_active` or subscribe to any recovery signal.

The signal is used internally for the cadence listener. Tests
import `recovery_state_signal` directly for dispatcher-level
assertions; no other production module does.

---

## Operation Mutex

The adapter enforces mutual exclusion between destructive buttons
via the `active_operation` field on `RuntimeData`. The field
carries the name of the operation currently running, or `None`
when nothing is active.

Distinct from Core's `recovery_active`:

- `active_operation` gates button presses for the duration of a
  single handler (seconds). It's the only gate on the button.
- `recovery_active` is a cadence signal, not a gate. HA reads it
  to switch the data coordinator's polling interval. The restart
  button does NOT read it — a user who sees a flakey modem mid-
  recovery may legitimately want to retry.

### Concurrency matrix

| Running | Attempted | Behavior |
|---------|-----------|----------|
| — (no active_operation) | restart | Allowed; `active_operation = "restart"` for the handler's duration (~2–5 s). |
| — (no active_operation) | reset | Allowed; `active_operation = "reset"`. |
| — (no active_operation) | refresh (user) | Allowed — runs normally. |
| `active_operation` set | any button | Refused (button disabled in UI; direct invocation logs and returns). |

`recovery_active` has no row because it doesn't participate in
gating. A button press during a recovery window is allowed; the
press goes through `active_operation` like any other.

### Context manager

Set/clear discipline lives in a single helper so both destructive
buttons share one code path.

```python
@contextmanager
def hold_active_operation(
    entry: CableModemConfigEntry,
    op: ActiveOperation,
) -> Iterator[None]:
    runtime = entry.runtime_data
    if runtime is None:
        raise OperationUnavailableError("runtime_data unavailable — entry is unloading")
    if runtime.active_operation is not None:
        raise OperationInProgressError(runtime.active_operation)
    runtime.active_operation = op
    try:
        yield
    finally:
        # Re-read — entry may have unloaded during the body.
        runtime = entry.runtime_data
        if runtime is not None:
            runtime.active_operation = None
```

Guarantees:

- The field is cleared on every exit path — success, exception,
  cancellation — because `finally` runs.
- Cleanup tolerates a concurrent entry unload that clears
  `runtime_data` to `None`.
- Uses `contextmanager` (not `asynccontextmanager`) because the
  set/clear itself is synchronous; the body is where awaits happen.
- No dispatcher signal fired from the mutex — `active_operation`
  is short-lived (seconds) and the buttons that read it don't need
  a signal (they only read the field when the user interacts with
  them). Core's `recovery_state_signal` handles the longer-lived
  window transitions.

### Diagnostics

`active_operation` is surfaced in the diagnostics download so a
stuck-state report is self-diagnosing. If the field is non-None
despite no handler actually running, the field's string value
identifies which code path left it set. `recovery_active` and the
recovery window's elapsed time are also exposed for the same
reason.

### Acceptance

The `active_operation` field and the `hold_active_operation` helper
are adapter-layer only — they gate destructive *buttons* for their
runtime (seconds). That is the ONLY button gate.

`orchestrator.recovery_active` is Core-scoped state set by the
recovery module when a window is open (from any trigger: command,
observed outage, reboot-signal match). Core's recovery observer fires the
dispatcher signal named by `recovery_state_signal(entry_id)` on
transitions. HA consumes it in one place only: the cadence listener
installed by `attach_recovery_cadence_listener` in `recovery_adapter.py`,
which drops the data coordinator's `update_interval` while a window
is open. Nothing else subscribes.

Core doesn't know *what* the observer does — it just invokes a
callable — which keeps the layering one-directional. No HA-side
component reads `recovery_active` for UX purposes: sensors render
snapshot truth, the button gates on the short-lived
`active_operation` mutex, and the user is trusted to decide when to
retry based on what the dashboard shows.

---

## Reset Entities Concurrency Guard

The Reset Entities button tears down and re-creates data-dependent
entities to pick up new channel IDs after a modem reboots to a
different channel set (UC-80). A second click while the first is
still running can fire `_handle_coordinator_update()` against
already-unloaded entities — observed symptom is an `AttributeError`
on `entry.runtime_data` after the entry is partially torn down.

The Reset button uses the shared `_hold_active_operation` context
manager (see § Operation Mutex) for its mutex discipline. The
button-specific availability toggle lives inside the `with` body:

```python
async def async_press(self) -> None:
    try:
        with _hold_active_operation(self._entry, "reset"):
            self._attr_available = False
            self.async_write_ha_state()
            try:
                # existing reset body — remove data-dependent entities,
                # re-register deferred listener, trigger refresh
                ...
            finally:
                self._attr_available = True
                self.async_write_ha_state()
    except OperationInProgressError:
        return  # another destructive operation is running
```

Three defences, all required:

1. **`active_operation` check at entry** (via the context manager) —
   a second click while any destructive operation is running refuses
   immediately. The field lives on `RuntimeData`, not the button
   instance, so it survives the temporary teardown of data-dependent
   entities during reset.
2. **`_attr_available = False` during the work** — the UI visibly
   disables the button so the user isn't tempted to hammer it.
3. **Null-safety** — the context manager re-reads `runtime_data` on
   exit because `async_unload_entry` can fire concurrently and clear
   it to `None`. The reset flow must not assume the entry is still
   loaded when it returns.

---

## Reauth Flow

When Core's auth circuit breaker opens (6 consecutive auth failures),
the adapter triggers HA's native reauthentication flow.

```text
Circuit breaker opens
 │
 ├─ 1. get_modem_data() returns AUTH_FAILED with circuit_breaker_open
 │
 ├─ 2. Adapter detects: snapshot.connection_status == AUTH_FAILED
 │     AND orchestrator.diagnostics().circuit_breaker_open == True
 │
 ├─ 3. Trigger: entry.async_start_reauth(hass)
 │     HA shows "Reauthentication required" notification
 │
 ├─ 4. User enters new credentials via async_step_reauth
 │     (reuses Step 3 connection form — host + credentials)
 │
 ├─ 5. Validation runs in executor
 │     (connectivity + auth + parse — same as config flow Step 4)
 │
 ├─ 6. On success:
 │     ├─ Update config entry with new credentials
 │     ├─ orchestrator.reset_auth()
 │     │   (clears streak, circuit, backoff, session)
 │     └─ Next poll attempts fresh login
 │
 └─ 7. On failure: show error, user retries
```

**No polling while circuit is open.** `get_modem_data()` returns
`AUTH_FAILED` immediately when the circuit breaker is open. The user
must fix credentials before polling resumes.

See ORCHESTRATION_USE_CASES.md UC-81 for the full scenario.

---

## Diagnostics Platform

The `diagnostics.py` module implements HA's diagnostics download.
Combines Core's `OrchestratorDiagnostics` with HA-side context.

**From Core (`orchestrator.diagnostics()`):**

Serialized via `OrchestratorDiagnostics.to_dict()` — all fields
included automatically when new diagnostics are added to the model.

- `poll_duration` — last poll wall-clock time in seconds
- `auth_failure_streak` — consecutive auth failures (0 = healthy)
- `circuit_breaker_open` — whether polling is stopped
- `session_is_valid` — auth manager session state
- `connectivity_streak` — consecutive connectivity failures
- `connectivity_backoff_remaining` — polls to skip before retry
- `resource_fetches` — per-resource timing and size from last
  successful collection (path, duration_ms, size_bytes per resource)
- `last_poll_timestamp` — monotonic time of last poll

**From HA (adapter-side):**

- PII review checklist
- Sanitized recent logs from both the HA adapter
  (`custom_components.cable_modem_monitor`) and Core package
  (`solentlabs.cable_modem_monitor_core`) loggers
- Runtime state summary (`modem_data` — connection + health only)
- Full `system_info` pass-through (all parser-extracted and computed fields)
- Full channel dump (`downstream_channels` + `upstream_channels`)
- Config entry details (host, protocol, supports_icmp, etc.)
- Coordinator state (last_update_success, update_interval)
- `active_operation` field — surfaces a stuck mutex in user reports
- Recovery window state — `recovery_active`, `recovery_reason`, and
  window elapsed seconds so a stuck-fast-poll report is
  self-diagnosing
- Generic auth diagnostics (per-strategy, not HNAP-specific)

**Sanitization:**

- Credentials, private IPs, MAC addresses, serial numbers scrubbed
- Uses `har_capture` library for HTML/content sanitization
- PII checklist warns user to verify before sharing

**No raw HTML capture.** Use `har-capture` for collecting raw modem
data for parser development.

### Diagnostics Top-Level Keys

The diagnostics output disassembles Core's nested `modem_data` dict
into separate top-level keys. The boundary between sections is
**source**: `modem_data` draws from snapshot and health evaluations
(orchestrator-derived state), while `system_info`, `downstream_channels`,
and `upstream_channels` are verbatim pass-throughs of Core's parser
output. The diagnostics builder never copies values from `system_info`
into `modem_data`.

| Key | Contents | Source |
|-----|----------|--------|
| `config_entry` | Host, protocol, model, credentials flag | HA config entry |
| `core_diagnostics` | Poll timing, auth state, circuit breaker | `orchestrator.diagnostics()` |
| `data_coordinator` | Last success, update interval | HA coordinator |
| `health_coordinator` | Last success, update interval | HA coordinator (conditional) |
| `modem_data` | Evaluated connection + health state | Snapshot + health probe |
| `system_info` | All parser-extracted and computed fields | `snapshot.modem_data["system_info"]` pass-through |
| `downstream_channels` | Per-channel data (sparse dicts) | `snapshot.modem_data["downstream"]` pass-through |
| `upstream_channels` | Per-channel data (sparse dicts) | `snapshot.modem_data["upstream"]` pass-through |

### `modem_data` — Evaluated State

The `modem_data` key contains **orchestrator-derived connection state
and health probe results**. Every field comes from `snapshot.*` or
`health_info.*` — evaluated assessments of modem reachability, not
raw parser output.

Modem identity (version, model), counters (error totals), and
measurements (channel counts, uptime) belong in `system_info` —
they are Core parser output, not HA-layer evaluations.

#### Connection State (from snapshot, enums converted to string values)

| Field | Type | Source |
|-------|------|--------|
| `connection_status` | string | `snapshot.connection_status` |
| `collector_signal` | string | `snapshot.collector_signal` |
| `error` | string | `snapshot.error` (empty on success) |

#### Health State (prefers health coordinator over snapshot)

| Field | Type | Source |
|-------|------|--------|
| `health_status` | string | Health probe result (`"none"` if unavailable) |
| `icmp_latency_ms` | float or null | ICMP round-trip (null if not supported) |
| `http_latency_ms` | float or null | HTTP response time (null if not attempted) |

### `system_info` — Parser Output

Verbatim pass-through of Core's `system_info` dict. Contains parser-
extracted fields, coordinator-computed counts, and aggregated totals.
This is the single source of truth for modem identity, counters, and
status.

Fields vary by modem (sparse dict). Common fields include:

| Field | Type | Origin |
|-------|------|--------|
| `downstream_channel_count` | int | Coordinator-computed (always present) |
| `upstream_channel_count` | int | Coordinator-computed (always present) |
| `total_corrected` | int | Aggregate or native (see PARSING_SPEC § Aggregate) |
| `total_uncorrected` | int | Aggregate or native (see PARSING_SPEC § Aggregate) |
| `docsis_status` | string | Parser-extracted or orchestrator-enriched (see below) |
| `software_version` | string | Parser-extracted |
| `system_uptime` | string | Parser-extracted |
| `model_name` | string | Parser-extracted (when available) |
| `hardware_version` | string | Parser-extracted (when available) |

#### `docsis_status` enrichment

`docsis_status` follows the same enrichment pattern as error totals:
the parser provides it when the modem exposes a native value, and the
orchestrator fills it in from channel `lock_status` when absent. If
neither the parser nor the orchestrator can determine the value (no
native field, no `lock_status` on channels), the field stays absent
in `system_info` — same sparse-dict rule as other fields. No sensor
is created.

1. **Parser provides it** — YAML `map` entries normalize vendor values
   to the canonical `"Operational"` (see SYSTEM_INFO_SPEC § Canonical
   Values). Non-mapped values pass through as raw diagnostic strings
   (e.g., `"Ranging"`).

2. **Parser does not provide it** — the orchestrator derives it from
   downstream channel `lock_status` fields and writes it into
   `system_info`. See RUNTIME_POLLING_SPEC § Status Derivation for
   the derivation rules (including when derivation is not possible).

One field, one location in the data layer. `snapshot.docsis_status`
reads from `system_info["docsis_status"]`, falling back to `"unknown"`
when the field is absent (used internally by the HA status cascade,
not exposed as a sensor).

---

## Services

All services are registered once on first entry setup and unregistered
when the last entry is removed.

### `generate_dashboard`

Generates Lovelace YAML for a complete modem dashboard based on
current channel data.

**Input options:**

- `device_id` (optional) — which modem to generate for. Defaults to
  first configured modem when omitted.
- Which graphs to include (DS power, DS SNR, DS frequency, US power,
  US frequency, errors, latency, status card)
- Graph timespan (hours)
- Channel label format
- Channel grouping (by direction, by type)

**How it works:**

1. Resolves target modem from `device_id` or falls back to first entry
2. Reads current channel data from `entry.runtime_data.data_coordinator`
3. Generates entity references for actual channels
4. Returns YAML string the user pastes into a manual dashboard card

### `request_refresh`

Triggers an immediate modem data poll, bypassing connectivity backoff.
Intended for automations that need on-demand polling (e.g., "ping
fails → trigger modem check").

**Fields:** Optional `device_id` (device selector filtered to
`cable_modem_monitor`). Falls back to all loaded entries when no
device is specified.

**Behavior:**

1. Resolve device_id to config entry
2. Short-circuit if `runtime.active_operation is not None` — a
   restart or reset is already running and will trigger its own
   post-operation refresh; no user action is needed
3. Call `orchestrator.reset_connectivity()` to clear backoff
4. Refresh health coordinator (if health monitoring is enabled)
5. Refresh data coordinator

Same logic as the "Update Modem Data" button — both use the shared
`async_request_modem_refresh()` helper to stay DRY, including the
`active_operation` gate. Internal refreshes (post-restart step 6,
health recovery listener) call the coordinator directly rather than
going through the helper.

**Automation example:**

```yaml
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.ping_gateway
      to: "off"
      for: "00:01:00"
  action:
    - service: cable_modem_monitor.request_refresh
      data:
        device_id: <modem_device_id>
```

### `request_health_check`

Triggers an immediate health check (ICMP + HTTP probes).

**Fields:** Optional `device_id` (device selector filtered to
`cable_modem_monitor`). Falls back to all loaded entries when no
device is specified.

**Behavior:**

1. Resolve device_id to config entry
2. If health monitoring is enabled, refresh health coordinator
3. If health monitoring is disabled, log a warning and return

**Automation example:**

```yaml
automation:
  trigger:
    - platform: state
      entity_id: binary_sensor.ping_gateway
      to: "off"
  action:
    - service: cable_modem_monitor.request_health_check
      data:
        device_id: <modem_device_id>
    - delay: "00:00:30"
    - service: cable_modem_monitor.request_refresh
      data:
        device_id: <modem_device_id>
```

---

## Config Entry Migration

Config entries evolve as the integration adds features and
restructures data.  Without migration, entries created by older
versions crash on startup because `async_setup_entry` expects keys
that don't exist.

**HA mechanism:** Config flows declare a `VERSION` class attribute.
When HA loads an entry whose stored version is lower than the current
`VERSION`, it calls `async_migrate_entry(hass, entry)` before
`async_setup_entry`.  The migration function transforms the entry
data and returns `True` (success) or `False` (failure — entry won't
load, user must reconfigure).

**Current version:** 2

**Design: auto-discovered migration registry.**

The `migrations/` directory uses convention-based discovery.  Drop a
file named `v{N}_to_v{M}.py` that exports `async_migrate(hass, entry)
-> bool`.  The registry discovers it automatically at import time —
no manual registration needed.  Migrations must be sequential
(M = N + 1).

`async_migrate_entry` walks the chain from the stored version to the
current version, applying each handler in sequence:

```text
stored v1 → v1_to_v2.async_migrate() → v2_to_v3.async_migrate() → current v3
```

Adding a future migration = one file.  No changes to dispatch logic.

**v1 → v2 key mapping:**

| v1 key | v2 key | Transform |
|--------|--------|-----------|
| `detected_manufacturer` | `manufacturer` | Rename |
| `detected_modem` | `model` | Strip manufacturer prefix |
| `detected_modem` | `user_selected_modem` | Copy as display name |
| `working_url` | `protocol` | Parse URL scheme; fallback: `legacy_ssl` → `"https"`, else `"http"` |
| `host` | `host` | Unchanged |
| `username` | `username` | Unchanged |
| `password` | `password` | Unchanged |
| `legacy_ssl` | `legacy_ssl` | Unchanged |
| `supports_icmp` | `supports_icmp` | Unchanged |
| `supports_head` | `supports_head` | Unchanged (default `false` if missing) |
| `entity_prefix` | `entity_prefix` | Unchanged (default `"none"` if missing) |
| `scan_interval` | `scan_interval` | Unchanged |
| — | `modem_dir` | Catalog lookup: manufacturer + model → relative directory path |
| — | `variant` | Default: `null` |
| — | `health_check_interval` | Default: 30 |

**`modem_dir` resolution:** The migration walks the catalog, reads
each `modem.yaml` for manufacturer and model names, and builds a
lookup table.  The v1 manufacturer and extracted model are matched
against this table (case-insensitive, including `model_aliases`).
The result is a relative path from the catalog root (e.g.,
`"arris/sb8200"`).  All config entry path construction uses
`modem_dir` — never manufacturer/model strings directly.

**Graceful failure:** If catalog lookup fails (modem removed or
manufacturer renamed beyond recognition), migration logs a warning
with the original values and returns `False`.  HA marks the entry as
failed — the user reconfigures through the setup wizard.

**v1 keys removed:** `parser_name`, `detected_manufacturer`,
`detected_modem`, `modem_choice`, `working_url`,
`parser_selected_at`, `docsis_version`, `actual_model`,
`auth_strategy`, `auth_form_config`, `auth_hnap_config`,
`auth_url_token_config`, `auth_discovery_status`,
`auth_discovery_failed`, `auth_discovery_error`, `auth_type`,
`auth_captured_response`.

---

## Testing

The adapter is modem-agnostic — its tests must be too.

**No modem-specific names.** Mock data uses generic names (`Solent
Labs`, `TPS-2000`, `TPS-3000`) — not real manufacturers or models. This
applies to all mock fixtures: entry data, catalog summaries, modem
identity, config flow selections, diagnostics titles, log messages.

**Mock at the Core/Catalog boundary.** Adapter tests mock the I/O
boundary (`load_modem_config`, `load_parser_config`,
`load_post_processor`, `list_modems`) and test wiring logic:
path dispatch, conditional construction, error propagation.
Do not parametrize over real catalog modems — that crosses the
layer boundary into catalog testing.

**Catalog tests own "every modem works."** The catalog test suite
(`test_modem_yaml_schema`, `test_modem_har_replay`) validates that
every modem config is valid and produces correct output through the
full orchestrator cycle. The adapter layer does not repeat this.

**Migration tests verify schema, not modem data.** Config entry
migration tests verify the key transform (v1 keys → v2 keys with
correct types and defaults). Use generic names for migration test
data. Catalog resolution algorithms (`resolve_modem_dir`) are tested
with synthetic `ModemSummary` data — not the real catalog.

---

## Module Inventory

The HA adapter layer consists of these modules:

| Module | Responsibility |
|--------|---------------|
| `__init__.py` | Startup, unload, migration dispatch, device registry, service registration |
| `coordinator.py` | `CableModemRuntimeData` dataclass + `CableModemConfigEntry` type alias |
| `recovery_adapter.py` | Recovery cadence listener — observer into Core + dispatcher signal that flips `update_interval` while a window is open |
| `sensor.py` | Entity classes for all sensor types |
| `button.py` | Restart, Update, Reset Entities buttons |
| `config_flow.py` | Setup wizard and options flow |
| `diagnostics.py` | Diagnostics download combining Core + HA-side data |
| `const.py` | Domain constants, config keys, defaults |
| `services.py` | `generate_dashboard` service handler |
| `migrations/` | Version-keyed config entry migration handlers |
| `core/log_buffer.py` | Log capture for diagnostics (HA adapter + Core package loggers) |
| `lib/host_validation.py` | URL building, host input parsing |
| `lib/utils.py` | Utility functions (e.g., uptime parsing) |

All modem-specific logic lives in Core and Catalog. The adapter
imports from `solentlabs.cable_modem_monitor_core` and
`solentlabs.cable_modem_monitor_catalog` — never from modem config
files or parser code directly.

---

## Distribution

Three packages, three delivery mechanisms. Lock-step versioning —
all three always share the same version number.

### What ships where

| Package | Delivery | Contains |
|---------|----------|----------|
| HACS zip (`cable_modem_monitor.zip`) | GitHub release asset | HA adapter: config flow, coordinator, sensors, buttons, services, translations, icons |
| Core (`solentlabs-cable-modem-monitor-core`) | PyPI wheel | Auth, parsers, orchestration, loaders, protocol, MCP tools |
| Catalog (`solentlabs-cable-modem-monitor-catalog`) | PyPI wheel | modem.yaml, parser.yaml for each supported modem |

The HACS zip contains only runtime files — `docs/` is excluded from
the zip build. Spec files stay in the repo for contributors but don't
ship to users.

### HACS configuration

`hacs.json` uses the default source archive download (no
`zip_release`). Branch-based installs (alpha testing via
`update.install` with `version: feature/v3.14.0`) require HACS to
download the source archive directly from GitHub — `zip_release: true`
breaks this path because HACS expects a zip asset on a GitHub release
that doesn't exist for branch refs. The `zip_release` optimization
(124 KB zip vs 3.4 MB source) will be re-enabled at stable release
when branch installs are no longer needed.

### Manifest loggers

`manifest.json` declares `loggers` for both PyPI packages:

```json
"loggers": [
    "solentlabs.cable_modem_monitor_core",
    "solentlabs.cable_modem_monitor_catalog"
]
```

This tells HA to include Core and Catalog log output when a user
enables debug logging for the integration. Without this field, only
the `custom_components.cable_modem_monitor` logger is captured.

### Install flow

1. HACS downloads the source archive from GitHub
2. HACS extracts `custom_components/cable_modem_monitor/` into place
3. HA reads `manifest.json` → sees `requirements` pins
4. HA pip-installs Core and Catalog from PyPI (exact `==` pins)
5. Integration loads

### Version pinning

`manifest.json` pins both PyPI packages with exact `==` specifiers.
This is deliberate — HA only checks whether the installed version
satisfies the specifier. It never queries PyPI for newer versions
within a range. A `>=` pin would leave users permanently on the
version installed at first setup. The `==` pin forces HA to upgrade
when the manifest changes.

All three packages are released in lock-step. `scripts/release.py`
bumps all version files atomically. Independent versioning is not
supported — even a catalog-only change (new modem config) triggers a
full release, because the user needs a HACS update (new manifest pin)
to receive the new catalog version.

### Release tiers

| Tag pattern | PyPI (publish.yml) | GitHub release (release.yml) | HACS visibility |
|-------------|-------------------|----------------------------|-----------------|
| `v*-alpha.*` | Core + Catalog published | Skipped — no release | Not visible. Side-load via branch tracking. |
| `v*-beta.*` | Core + Catalog published | Prerelease + zip asset | Users with "Show beta versions" enabled |
| `v*.*.*` (stable) | Core + Catalog published | Release + zip asset | All users |

PyPI publishing happens for all tiers. The GitHub release (and thus
HACS visibility) is what varies by tier.

### Rollback safety

HACS reads `hacs.json` from the default branch to determine download
strategy. When `zip_release` is eventually re-enabled for stable
release, HACS will expect a zip asset on every release — including
older ones a user might roll back to. Prior stable releases must have
a `cable_modem_monitor.zip` asset uploaded before `zip_release: true`
reaches the default branch. See P-merge in the alpha roadmap.

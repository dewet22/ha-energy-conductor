# Plan: Programmatic Eddi (+ experimental Zappi) boost-schedule editing in pymyenergi

## Context

You want to edit your Eddi's recurring boost **schedules** programmatically. The
investigation answered the underlying question first:

- **pymyenergi exposes NO schedule-editing hook.** It only *reads* the schedule:
  `Zappi.fetch_boost_data()` (`zappi.py:47-52`) does
  `GET /cgi-boost-time-Z{serial}` and stores the raw, *unparsed* JSON. The only
  `cgi-boost-time` reference in the whole library is that single read — verified
  by grep. What it *does* expose is **immediate/ad-hoc** boost commands
  (`start_boost`, `start_smart_boost`, `stop_boost`, Eddi `manual_boost`) — not
  persistent timer slots. Eddi has no schedule read at all today.
- **There is a public reference, but community-maintained, not official.**
  `api-docs.s18.myenergi.net` is a skeleton with no schedule endpoints. The
  usable source is **twonk/MyEnergi-App-Api** (reverse-engineered). Key asymmetry
  it documents: **the Eddi write endpoint is specified; the Zappi write endpoint
  is NOT** (marked "still to come"). So Eddi is on documented ground; Zappi is
  inferred-by-analogy and must be treated as experimental.

Goal: add schedule read+write to the **library** (the correct home — it's where
every other device command lives and makes it reusable), driven near-term from a
standalone script. Exposing it as an HA service in ha-myenergi, or wiring it into
energy_conductor, is explicit follow-up, not this plan.

### The boost-schedule API (verbatim from twonk/MyEnergi-App-Api)

- **READ** `GET /cgi-boost-time-{prefix}{serial}` →
  `{"boost_times": [{"slt":11,"bsh":7,"bsm":30,"bdh":0,"bdm":0,"bdd":"01111100"}, ...]}`
  - `slt` slot id · `bsh`/`bsm` start hour/min · `bdh`/`bdm` duration hour/min ·
    `bdd` = 8-char day bitmask (char 0 unused; chars 1-7 = Mon..Sun; "1"=active).
  - **Eddi: 8 slots** (11,12,13,14,21,22,23,24). **Zappi: 4 slots** (11-14).
- **WRITE (Eddi, documented)**
  `GET /cgi-boost-time-E{serial}-<slot>-<start>-<duration>-<dayspec>`
  - **start and duration are encoded as `60*hours + minutes` integers** (NOT HHMM).
    e.g. 07:30 start → `450`; 0h45m duration → `45`.
  - dayspec is the same 8-char `bdd` string.
- **WRITE (Zappi)** — inferred identical shape, **UNVERIFIED**.
- **The core trap:** READ returns start/duration as split hour+minute fields;
  WRITE takes them as single `60*h+m` integers. All conversion must be contained
  and unit-tested offline.

## Where the work happens

The installed pymyenergi (`…/ha-myenergi/.venv/lib/python3.13/site-packages/pymyenergi/`)
is a **copied dist, not editable and not version-controlled** — edits there would
be lost on any reinstall. So, mirroring the ha-myenergi workflow:

1. `git clone` your pymyenergi fork (or `CJNE/pymyenergi`) to `~/git/pymyenergi`,
   branch e.g. `feat/boost-schedule`.
2. Implement + unit-test there.
3. Deploy into the venv for live hardware testing via
   `uv pip install -e ~/git/pymyenergi` (make the venv copy editable), or copy the
   changed files across.
4. Eventual upstream PR from the fork — same probe pattern as #767.

## Implementation (file-by-file, matching existing style: async, minimal typing,
`await self._connection.get(...)`, `return True`)

### 1. NEW `pymyenergi/boost_schedule.py` — `BoostSlot` value object
Pure, network-free, the home of the read/write encoding asymmetry.
```
@dataclass
class BoostSlot:
    slot_id: int
    start_hour: int; start_minute: int
    duration_hour: int; duration_minute: int
    days: str            # raw 8-char bdd, kept opaque end-to-end
```
- `start_offset` → `60*start_hour + start_minute` (write-form)
- `duration_total` → `60*duration_hour + duration_minute` (write-form)
- `from_api(d)` classmethod ← one READ dict (`slt/bsh/bsm/bdh/bdm/bdd`)
- `from_components(slot_id, start_offset, duration_total, days)` ← inverse,
  via `divmod(x, 60)`
- `is_empty` → all-zero start/duration and all-zero `days` (slots always return
  all 8/4 entries; this distinguishes populated from unused).

### 2. `pymyenergi/base_device.py` — shared, prefix-agnostic methods
Endpoint shape is identical per device, so put mechanics on `BaseDevice`:
- `boost_slot_ids` — overridable, default `[]`; the single point encoding
  "Eddi 8 / Zappi 4".
- `async def fetch_boost_times(self)` →
  `GET /cgi-boost-time-{self.prefix}{self._serialno}`, parse `boost_times` into
  `list[BoostSlot]` via `from_api`, return it.
- `async def set_boost_time(self, slot)` → validate `slot.slot_id in
  self.boost_slot_ids` (raise `ValueError` otherwise — cheap guard against
  malformed writes), then
  `GET /cgi-boost-time-{prefix}{serial}-{slot_id}-{start_offset}-{duration_total}-{days}`,
  `return True`.
- Optional thin `set_boost_times(slots)` = sequential loop (no concurrency).
  Do NOT invent a bulk endpoint — none exists.

### 3. `pymyenergi/eddi.py` — primary target
- `EDDI_BOOST_SLOT_IDS = [11,12,13,14,21,22,23,24]`; override `boost_slot_ids`.
- Inherits fetch/set unchanged. Optionally populate a `boost_times` attribute in
  a `refresh()` override (parallel to Zappi) — keep optional; explicit
  `await eddi.fetch_boost_times()` suffices for scripting.

### 4. `pymyenergi/zappi.py` — experimental
- `ZAPPI_BOOST_SLOT_IDS = [11,12,13,14]`; override `boost_slot_ids`.
- Keep existing raw `fetch_boost_data` (HA integration may use it); new parsed
  method is additive.
- **Loudly flag the Zappi write as unverified** (docstring/comment) — format
  inferred from Eddi, not in the community docs.

## Consumption today: standalone script (spec, build after library lands)
Run with the venv interpreter once pymyenergi is editable there.
- Secrets from env (`MYENERGI_SERIAL`, `MYENERGI_PASSWORD`) — never hardcode/write.
- `conn = Connection(username=serial, password=password)` (ASN routing automatic).
- `eddi = Eddi(conn, eddi_serial)` — construct directly; no MyenergiClient needed.
- Drive the verification sequence below.

energy_conductor consumption later means adding `pymyenergi` to its
`manifest.json` requirements + constructing a `Connection` from config — real,
separate, different secret-storage story than its current entity-only writer.
Flagged, not built.

## Verification (read-modify-verify on real hardware — schedule writes are
destructive and have no rollback)
1. **Backup:** `await eddi.fetch_boost_times()`; print every slot (raw JSON +
   write-form). This printout is the manual restore path — save it before any write.
2. **Pick a throwaway slot:** an `is_empty` Eddi-only id (e.g. `24`) confirmed
   empty from step 1, so a mistake can't disturb a real entry.
3. **Offline encoding check (before hardware):** assert e.g. 07:30/0h45m/"01111100"
   → `start_offset==450`, `duration_total==45`, and `from_components` round-trips
   back to 7,30,0,45. Fail fast otherwise. (Also a pytest unit test in the fork.)
4. **Write one slot:** `await eddi.set_boost_time(test_slot)`.
5. **Re-fetch & verify:** confirm the slot reads back with matching
   `bsh/bsm/bdh/bdm/bdd` — closes the write(`60*h+m`)→read(split fields) loop.
6. **Restore:** rewrite the slot's original contents from step 1 (or leave empty);
   re-fetch to confirm.
7. Only then edit real slots. One `await` at a time; never loop-write during
   verification.

## Sequencing
1. `boost_schedule.py` + offline pytest (pure, no hardware).
2. base_device methods. 3. Eddi slot ids. 4. live read-modify-verify on slot 24.
5. Zappi slot ids last, labelled experimental.

## Pitfalls
- Venv pymyenergi is a non-editable copied dist — do the work in a git checkout,
  then make the venv editable; otherwise edits vanish on reinstall.
- Keep `bdd` an opaque 8-char string; do not reinterpret the bitmask.
- The split-fields (read) vs `60*h+m` (write) asymmetry is the one error-prone
  spot — fully contained in `BoostSlot`, covered by the round-trip assert.
- Zappi write is inferred; verify on hardware before trusting, or leave gated.

# Migrating Huawei Solar to `modbus-connection`

This integration is a thin consumer; all the Modbus lives in
[`huawei-solar-lib`](https://github.com/wlcrs/huawei-solar-lib), so that is where
the migration happened. Both repos are migrated on the
`modbus-connection-migration` branch of their `balloobbot` forks.

**What moved.** The 744-entry table of `RegisterDefinition` objects became 23
`modbus_connection.model` components; the tmodbus client and its register-aware
subclass became a shared `HuaweiModbusConnection` with one unit per sub-device;
`registers.py`, `register_client.py`, `modbus_client.py` and the whole
`register_definitions/` package are gone. The integration's 449 register-name
call sites still address registers by the name Huawei publishes — that is what
Home Assistant stores in an entity's config — resolved through a generated
registry onto component fields.

**What verifies it.** None of this can be checked against real hardware, so
`tests/fixtures/legacy_decode_vectors.json` freezes what the *old* decoders
returned for 5952 word patterns across all 744 registers — including the
all-ones pattern that trips each register's "value not available" sentinel — plus
the words all 107 writable registers used to encode to. The suite replays those
through the new fields: 1649 tests, all green, and every address, width and
writability compared against the old table. On top of that, all 228 of the
integration's entity descriptions are checked to resolve to real registers, and
a smoke run reads the full 193-register sensor set in 23 block reads.

---

## 1. What weird things does this library do?

**Login is a private function code, and it is mutual.** Everything privileged
rides FC `0x41`. Sub-function `0x24` asks the inverter for a 16-byte challenge;
`0x25` answers with `HMAC-SHA256(SHA256(password), challenge)` — and the inverter
answers back with a digest over the *client's* challenge, which the library
verifies. Fail that check and it raises rather than proceeding, on the grounds
that something is impersonating the inverter. That is a real mutual
authentication handshake sitting inside Modbus.

**There is a heartbeat, and it is a register write.** Write `1` to register
`49999` every 15 seconds or the login session lapses and every write starts
failing. The library also fires one immediately *before* each write, and the
write path retries the whole login on a permission error — belt, braces, and a
second pair of braces, because the session can lapse between the heartbeat and
the write.

**Permission denied is a vendor exception code.** `0x80`, outside the standard
set. And it is not reliable: some firmware answers a forbidden write with a
plain `ServerDeviceFailure` instead, so `has_write_permission()` treats both as
"no" — and probes by reading the time zone and writing that same value back to
itself.

**Bulk data comes as files, not registers.** Optimizer history is fetched with a
start request (answering with total length and frame size), one request per
frame, and a completion request returning a CRC16 the client checks — *with the
bytes the other way round* from how it computes it. And the query emulates the
FusionSolar app: ask for the last 600 seconds and take the newest record.

**Sub-devices are enumerated over FC 0x2B.** Device code `3`, object id `0x87`,
answering with `key=value;key=value` strings — a device inventory smuggled
through Read Device Identification.

**Three registers have two names each.** `32066` is `grid_voltage` on a
single-phase inverter and `line_voltage_A_B` on a three-phase one; `32072` is
`grid_current`/`phase_A_current`; `40000` is both `system_time` (decoded as a
timestamp) and `system_time_raw` (the same words as an integer). Two fields,
one address.

**The second battery is not a copy of the first.** Every field of storage unit 2
sits at its own arbitrary offset from unit 1 — `+730`, `+747`, `+742`, `+648`,
and for the software version `−15`, i.e. *backwards*. It is a repeat in name
only.

**Schedule tables are packed by byte, not by register.** The peak-shaving table
is a count register followed by 14 entries of `>HHIB` — nine bytes each — so
entries straddle register boundaries and the whole 64-register block has to be
decoded as one byte string.

**Strings are padded with garbage.** Not zeroes: whatever was in the buffer. So
everything from the first NUL on is dropped rather than stripped, and the text is
UTF-8 rather than ASCII.

**Timestamps have no time zone, and the offset may live on another device.** The
inverter reports a naive local epoch; the library subtracts the configured time
zone and another hour if DST is in effect. When the inverter sits behind an EMMA
it does not report its own time at all, and the time is read from the EMMA.

**Querying an offline power meter makes the inverter hang up.** During an outage
with a backup box the meter goes offline, and reading its registers causes the
*inverter* to close the Modbus connection. So the library tracks meter state,
filters those registers out of the poll while it is offline, re-probes
`METER_STATUS` before including them again, and resets the flag whenever the
device status changes — because a status change is the signal that an outage
started.

**Every read is "not available" first.** Each width has its own sentinel
(`0xFFFF`, `0x7FFF`, `0xFFFFFFFF`, …) meaning no value, checked before decoding.

**And the gain is a divisor.** Huawei publishes a gain and the value is
`raw / gain`, which matters more than it sounds — see below.

---

## 2. What internals of `modbus-connection` did I have to touch?

Nothing was monkeypatched. Two subclasses, three private attributes, one
reach-around.

**`TmodbusUnit._conn`, `._client` and `_conn._pacer`** — the whole FC `0x41`
surface. `ModbusUnit` has no raw-PDU seam, so `HuaweiUnit.execute_pdu()`
subclasses `TmodbusUnit`, reaches through `self._conn` to `connect()`, takes
`self._conn._pacer.paced(self._unit_id)` so a PDU queues behind ordinary reads
instead of racing them, and calls `self._client.execute(pdu)`. Six lines, two
`noqa: SLF001`, and it locks this library's login and file reads to the tmodbus
backend while everything else stays backend-neutral. The library feature-detects
it through a `SupportsHuaweiPdu` Protocol, so a pymodbus or mock unit still
serves every register and raises a clear error for the rest.

**`modbus_connection.tmodbus.ModbusConnection` and `TmodbusUnit`, subclassed** —
to return the unit above from `for_unit()`, and to add the retry policy the
library used to get from tmodbus's `response_retry_strategy`.

**`Component.register_ranges = None` after `restrict_fields()`** — a documented
attribute, but set to work around what `restrict_fields` did (finding 5 below).

**`MockModbusUnit._ensure_connected()`** in tests, to bring the mock up before
asserting on a write.

Everything else was public and held: `NumberField` and `RegisterField` subclass
cleanly, `encode_int` did what it says, and the read planner, `ComponentGroup`,
the mock and the pytest plugin were used exactly as documented.

---

## 3. What could `modbus-connection` do better?

Ordered by how much they cost here.

### 3.1 A raw-PDU seam — or failing that, a supported way to subclass a unit

The review tracks this as gap **B** and marks it *not planned*, on the grounds
that the only surveyed consumer was a fuzzing framework. That undercounts it:
this library needs it for **login, the entire optimizer feature, and device
discovery** — three user-visible features, not diagnostics.

The cheap fix is not a new `execute_raw()` on the protocol. It is to make
`TmodbusUnit` **subclassable on purpose**:

```python
class TmodbusUnit:
    @property
    def client(self) -> AsyncModbusClient: ...   # today: _conn._unit_client(...)
    @property
    def connection(self) -> ModbusConnection: ... # today: _conn
    def paced(self) -> AbstractAsyncContextManager[None]: ...  # today: _conn._pacer.paced(unit_id)
```

With those three public, my sidecar is ten lines of ordinary code with no
`noqa`. The backend-specific escape hatch stays backend-specific — which is
right — but stops being a private-attribute reach-around. A one-paragraph README
note saying "to add a vendor function code, subclass your backend's unit" would
turn a wall into a documented seam.

### 3.2 `read_device_identification()` cannot ask for anything but the basics

```python
async def read_device_identification(self) -> dict[int, bytes]: ...   # protocol
return await self._client.read_device_identification(1, 0)            # tmodbus backend
```

No arguments, and the backend hardcodes device code 1, object id 0. Huawei's
sub-device inventory is at `(3, 0x87)`. This is not an exotic function code —
it is *the same* function code with the parameters the spec defines, and the
signature makes two thirds of it unreachable. It should be
`read_device_identification(device_code=1, object_id=0)`. This one is a bug
fix, not a design change, and it would have removed one of my two reach-arounds.

### 3.3 Vendor exception codes are erased by the model layer

`ModbusExceptionError.from_code(0x80)` gives the base class, which is fair
enough. But `ReadPlan.execute` catches whatever was raised and **re-creates it**
through `from_code`, so even an exception a custom unit deliberately raised is
flattened on the way out. Verified:

```python
unit.fail_read(10, VendorError(0x80, "permission denied"))   # VendorError(ModbusExceptionError)
await component.async_update()
# raised ModbusExceptionError, code=128
```

The code survives; the type does not. Consumers are pushed back to comparing
`exception_code` against magic numbers — exactly what the typed hierarchy exists
to avoid. tmodbus has `register_custom_exception`; this has no equivalent.
Either let `from_code` consult a registry, or re-raise the original exception
with the block attached instead of rebuilding it.

### 3.4 Retry policy is hardcoded off and cannot be configured

`retry=retry_never` in the tmodbus backend, `retries=0` in pymodbus, and no
parameter anywhere. This library has always retried a timed-out request three
times with exponential backoff, and inverters behind an SDongle genuinely need
it. Synthesising it in the owner means wrapping unit methods one by one — I
wrapped the three this library issues, which is honest but leaves the other
sixteen unretried, and any consumer doing the same will make the same
compromise differently.

`ModbusConnection(..., retries=3)` — or a `retry_on_timeout` policy object —
belongs next to `timeout` and `message_spacing`. It is the same category of
knob: something only the connection can enforce.

### 3.5 `restrict_fields()` excludes by address, which breaks aliased registers

Concrete, and it cost me an afternoon. `_reshaped_ranges()` collects the
addresses of dropped fields and marks them unreadable. When two fields share an
address — `grid_voltage` and `line_voltage_A_B` at 32066 — dropping one punches
a hole at an address the *kept* one still reads. The block splits around a
register that is being read anyway:

```
before:  (32064, 2) (32066, 1)  ->  read 32064 count 3
after:   (32064, 2) (32066, 1)  ->  read 32064 count 2 ; read 32066 count 1
```

The fix is one line: exclude `dropped_addresses - kept_addresses`, not
`dropped_addresses`. My workaround — clearing `register_ranges` after
restricting — only works because these components declare no ranges; a device
that does declare them has no way out.

### 3.6 `restrict_fields()` is one-way, and `ComponentGroup` does not notice it

It only ever narrows, so a consumer whose field set changes — which is every
Home Assistant integration, because users enable and disable entities — must
throw the components away and build new ones. And `ComponentGroup` snapshots its
ranges in `__init__` and caches its plan, so restricting a member afterwards
leaves the group planning against the old shape. Rebuilding both on every change
is what I do; it works, but the API reads as though narrowing is a live
operation and it is not. Either `restrict_fields(None)` to reset, or make it
return a narrowed view and leave the component alone.

### 3.7 Scale multiplies; a lot of vendors publish a divisor

`gauge(address, scale)` computes `raw * scale`. Huawei publishes `gain` and the
value is `raw / gain`. Passing `1 / gain` is *nearly* right and my golden-file
test caught where it is not: for a 64-bit energy counter over a gain of 1000,
`raw * 0.001` and `raw / 1000` differ in the last bits. The review already
flagged this for Sigenergy and Huawei. Accept a `divisor=` alongside `scale=`,
or take a `Fraction`. It is three lines in `_ScaledField._scale` and it removes
a whole class of "why is the last digit different" bug reports.

### 3.8 An unrecognised enum value is always "missing", never an error

`NumberField._convert` warns once and decodes `None`. That is a good default —
better than what this library did — but it is not always right, and there is no
way to ask for the other behaviour. This library treats an unknown device state
as an error so a new firmware is noticed rather than silently blanked, so I had
to wrap every converter in a function that re-raises as a non-`ValueError`,
relying on `_convert` only catching `ValueError`. That is a trick, not an API.
`enum(..., on_unknown="raise")` would be one keyword.

Wrapping the converter also **hides the enum class**, which consumers need:
building a `select` entity means enumerating the members. `NumberField.__init__`
accepts `enum_type=` but assigns it straight to `convert` and keeps no reference,
so even without my wrapper there is nothing to read back. Keep
`self.enum_type = enum_type`.

### 3.9 `ModbusUnit` does not say which unit it addresses

`TmodbusUnit._unit_id` is private and the protocol has no `unit_id`. Consumers
key devices, log lines and diagnostics by unit id — this one did, in seven
places. I now thread it through the device constructor separately, which means
it can disagree with the handle. A read-only `unit_id` on the protocol costs
nothing.

### 3.10 Fields cannot be write-only, and every field enters the read plan

Huawei has four command registers — `startup`, `shutdown`, `sdongle_reset`,
`sdongle_connection_port` — that the device refuses to *read*. There is no
`readable=False`, and a component reads every field it declares, so one of these
in a polled component poisons its whole block. I put them in a `Commands`
component that is simply never updated. That works, but "declare it and never
call `async_update`" is a convention, not a guarantee; `writable=True,
readable=False` would say it in the model.

### 3.11 `write_register()` discards the device's echo

FC 06 echoes the value written, and this library used to compare it and report
whether the write took. The protocol returns `None`, so `set()` now returns
`True` unconditionally and a caller that wants verification has to re-read.
Returning the echoed value — or an explicit `verify=True` — would keep that.

### 3.12 Smaller things

- **`on_connection_lost` is synchronous**, so any async recovery (re-login here)
  has to be spawned as a task that races the connect-on-demand of the next
  request. It happens to be safe here because a request without a session comes
  back as a permission error that retriggers login. An awaitable callback, or a
  documented "the first request after a drop may run before your callback
  finishes", would remove the sharp edge.
- **`RegisterField` is a descriptor**, so a value *typed* as one and reached by
  attribute access is re-typed by mypy as the value it decodes to. A
  `RegisterLocation.definition` property came back as `Any | None`; I made it a
  method to sidestep it. Separating the descriptor from the field spec would fix
  this properly.
- **`decode(words, scale_exponent)`** forces every custom field to accept a
  parameter only SunSpec-style dynamic scaling uses. Ten of my field classes
  ignore it, and the repo's linter flags each one.

### What worked, and is worth saying

Connect-on-demand with no self-reconnect **deleted code**. The old library
reached into `transport.base_transport.send_and_receive` for login for one
reason: re-login ran from tmodbus's reconnect callback, while the transport held
its own lock, so going through the normal path deadlocked. With no invisible
reconnect there is no callback, no lock held, and no hack — the comment
explaining it is gone along with the workaround. The review predicted exactly
this, and it was right.

`message_spacing` and `connect_delay` were precisely the two knobs needed and
map one-to-one onto what tmodbus's smart transport was configured with. The
block planner reproduced the hand-rolled batcher exactly once `max_gap=15` and
`max_span=65` were set, so ~60 lines of batching logic went away. `MockModbusUnit`
and the pytest plugin replaced a bespoke transport stub and made the device
tests better than they were: detection now drives real Modbus exceptions through
the real probe chain instead of a stubbed `client.get`.

And the shared connection is the thing this library most wanted. Sub-devices used
to be a cloned client over a shared transport; they are now
`connection.for_unit(id)`, which is what the daisy chain has always physically
been.

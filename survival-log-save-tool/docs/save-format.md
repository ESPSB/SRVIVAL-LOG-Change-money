# How the save format was reverse engineered

Notes from figuring out a Unity + il2cpp game save that had no public documentation and
no community save editor. Written with *Survival Log* as the example, but the steps are
generic and the format facts are not.

## 1. Ruling things out

The save is a single ~200 KB binary blob. It is **not**:

| Candidate | Why not |
|---|---|
| JSON / XML | not text |
| Unity `YAML` | no `%YAML` header |
| .NET `BinaryFormatter` | header does not carry the NRBF magic `00 01 00 00 00 FF FF FF FF` |
| Protobuf / MessagePack / BSON | string framing does not match any of them |

What it *does* look like, on first inspection:

```
4A | EC FF FF FF | 13 00 00 00 | "2026_10_03_20_04_11"
   | E1 FF FF FF | 1E 00 00 00 | "Save_2026_10_03_20_04_11.bytes"
```

Two things stand out: every string is prefixed by a *negative* int32 whose absolute value
is `len + 1`, followed by a **second** int32 that is the UTF-16 character count, and the
file starts with a lone byte (`0x4A`) before the first string. Those two oddities are the
fingerprint that eventually identified the format.

## 2. Identifying the serializer

A Unity + il2cpp game keeps its managed types in `global-metadata.dat`, so the class and
field names are readable even though the code is compiled to C++. Dumping the game's
types and grepping them shows every save class carries an attribute:

```csharp
[MemoryPackable(GenerateType::Object (0))]
public class GameSaveData : IConfig, IMemoryPackable<GameSaveData>, IMemoryPackFormatterRegister
{
    private sealed class GameSaveDataFormatter : MemoryPackFormatter<GameSaveData> { ... }
    ...
}
```

That settles it: the save is serialized by **MemoryPack**.

## 3. The wire format

Taken from [Cysharp/MemoryPack](https://github.com/Cysharp/MemoryPack) and confirmed
against the bytes:

| Value | Encoding |
|---|---|
| object | 1 byte member count, `0..249`. `255` = null object. (`250`–`254` are reserved: wide tag, reference id, …) |
| collection / array / list | int32 length. `-1` = null, `0` = empty |
| `string` | `int32 ~utf8ByteCount`, `int32 utf16Length`, then the UTF-8 bytes. `0` = empty, `-1` = null |
| `bool` | 1 byte |
| `int32` / `int64` / `float` / `double` | 4 / 8 / 4 / 8 bytes, little endian |
| enum | its underlying integer type |

`~x` is bitwise NOT, i.e. `~n == -(n + 1)` — which is exactly the weird `len + 1`
negative prefix seen in step 1. `MemoryPackWriter.WriteUtf8` documents the layout
inline as `(int ~utf8-byte-count, int utf16-length, utf8-bytes)`. The character count is
stored alongside the byte count so the reader can size the target string without
scanning.

An object header is a *member count*, and a member count of 74 on the first byte of this
save is a good sanity check: the class it represents really does have 74 serializable
members.

### Why the layout is fully derivable

MemoryPack's generated serializer writes members in **declaration order**, and for
`GenerateType.Object` it writes all of them. il2cpp metadata stores fields in declaration
order too. So the file layout is a pure function of the type dump — no binary analysis,
no disassembly, no debugger needed.

## 4. Reading the type dump

Cpp2IL's `--output-as diffable-cs` emits one `.cs` file per type, in a directory tree that
mirrors the namespace, with field offsets as comments:

```
private string <Name>k__BackingField; //Field offset: 0x10
public int InitChapterId;             //Field offset: 0x24
```

Two things matter when parsing these files:

1. **Key classes by full name, not simple name.** Games reuse simple names across
   namespaces (`DynamicNpc` existed in both `GameCore.HotUpdate` and
   `GameCore.HotUpdate.Battle.Logic` with completely different layouts). The directory
   path gives you the namespace for free.
2. **Resolve field types in the declaring class' namespace**, walking up the namespace
   chain before falling back to a global name lookup.

Generated members appear as `<PropertyName>k__BackingField`; strip that to get the real
property name when printing paths.

## 5. Verifying offsets without running the game

A parser that has silently drifted off by a few bytes still produces plausible output,
because most of a save is zeros and small integers. Three cheap checks make the result
trustworthy:

1. **Object boundaries.** Nested objects that parse to a *known member count* must end
   exactly where the next object's header byte appears. Two adjacent `0xC2 0x94` bytes
   (`194`, `148` – the member counts of two known classes) at a predicted boundary is
   strong evidence the preceding N members were all sized correctly.
2. **String anchors.** A MemoryPack string is self-describing: header, character count,
   and UTF-8 payload must agree. Landing exactly on a well-formed, human-readable string
   where the schema predicts a string field is essentially unforgeable.
3. **Cross-check with runtime logs.** The game's own log file printed a monotonic
   "max money" baseline of `1580`; the same value turned up at the predicted offset of
   `PurchaseSafetyState.DisasterInitialMoney`, confirming the whole chain.

Do not trust a single anchor. Two anchors *straddling* the target field — one before it
and one after it — pin both ends of the region, so an error anywhere in between would
have to be self-cancelling.

## 6. Appendix: il2cpp `Il2CppTypeDefinition` (metadata v31)

If you want to read `global-metadata.dat` directly instead of dumping with Cpp2IL, note
that most second-hand tables of this struct are wrong for v31. The layout that actually
checks out byte-for-byte is:

| Offset | Field | | Offset | Field |
|---|---|---|---|---|
| `+0` | `nameIndex` (int32) | | `+44` | `propertyStart` |
| `+4` | `namespaceIndex` (int32) | | `+48` | `nestedTypesStart` |
| `+8` | `byvalTypeIndex` (int32) | | `+52` | `interfacesStart` |
| `+12` | `declaringTypeIndex` (int32) | | `+56` | `vtableStart` |
| `+16` | `parentIndex` (int32) | | `+60` | `interfaceOffsetsStart` |
| `+20` | `elementTypeIndex` (int32) | | `+64` | `method_count` (uint16) |
| `+24` | `genericContainerIndex` (int32) | | `+66` | `property_count` |
| `+28` | `flags` (uint32) | | **`+68`** | **`field_count`** (uint16) |
| **`+32`** | **`fieldStart`** (int32) | | `+70` | `event_count` |
| `+36` | `methodStart` (int32) | | `+72` | `nested_type_count` |
| `+40` | `eventStart` (int32) | | `+74` | `vtable_count` |
| | | | `+76`/`+78` | `interfaces_count` / `interface_offsets_count` |
| | | | `+80` | `bitfield` (uint32) |
| | | | `+84` | `token` (uint32) |

Total size 88 bytes (`typeDefinitionsSize / 25033 == 88`). `byrefTypeIndex` is present
only for metadata ≤ 24.5, so `declaringTypeIndex` sits at `+12`, not `+16`.

`fieldStart` is at `+32` and `field_count` at `+68` — off-by-four errors here are the
usual reason a hand-rolled metadata parser "almost works".

# mp-save-tool

Locate and edit fields inside a **MemoryPack** save file — without the game binary,
without a debugger, and without Cheat Engine.

The tool takes two inputs:

1. a **type dump** produced by [Cpp2IL](https://github.com/SamboyCoding/Cpp2IL)
   (`--output-as diffable-cs`), and
2. the **save file** itself.

Because [MemoryPack](https://github.com/Cysharp/MemoryPack) writes object members in
declaration order and the dump carries both that order and the field types, the whole
byte layout can be reconstructed on paper. `mp_reader.py` does exactly that and tells
you the file offset of any field you name.

It is game-agnostic: any Unity + il2cpp game that saves with MemoryPack works the same
way. It was developed against *Survival Log* (生存日志, LLS / Lilith Games).

## Requirements

- Python 3.8+ — standard library only, no `pip install` needed
- Windows/Linux/macOS
- A one-time [Cpp2IL](https://github.com/SamboyCoding/Cpp2IL/releases) run on the game
  (Windows binary is easiest; it needs the .NET runtime bundled in its release)

## Quick start

### 1. Dump the game's types (once)

```bash
Cpp2IL.exe --game-path "/path/to/GameFolder" \
           --exe-name  "Game" \
           --output-as diffable-cs \
           --output-to out
```

You end up with `out/DiffableCs/<Assembly>/<Namespace folders>/<Class>.cs`.

> If Cpp2IL cannot find the il2cpp registration data automatically, pass
> `--force-binary-path` / `--force-metadata-path` explicitly. Il2CppDumper's
> auto mode does **not** work on Unity 6; Cpp2IL does.

### 2. Find the field you want

```bash
# what classes exist?
python3 mp_reader.py --schema-dir out/DiffableCs classes "SaveData$"

# what does one class look like, in serialized order?
python3 mp_reader.py --schema-dir out/DiffableCs members GameCore.HotUpdate.AgentSave

# where is it in the save file?
python3 mp_reader.py --schema-dir out/DiffableCs find --save Save.bytes Money
```

```text
0x001160  $.CurSave.LeadingRole.Money   ('i32',)   in GameCore.HotUpdate.AgentSave
```

### 3. Read / write

```bash
python3 mp_reader.py read  --save Save.bytes 0x1160 --type int32
python3 mp_reader.py patch --save Save.bytes 0x1160 1000 --type int32
```

`patch` writes a `*.bak-before-patch` backup next to the save before touching anything.
Always close the game first.

## Commands

| Command | Purpose |
|---|---|
| `classes [REGEX]` | list classes found in the dump |
| `members CLASS` | list one class' members **in serialized order**, with types |
| `walk --save FILE [--root CLASS]` | walk the object graph, print the object tree and where the walk stopped |
| `find --save FILE PATTERN [--regex]` | walk and print the file offset of every matching field |
| `read --save FILE OFFSET [--type T]` | read a primitive at an offset |
| `patch --save FILE OFFSET VALUE [--type T]` | write a primitive at an offset |

Offsets accept `0x1160` or decimal. Types: `byte`, `int16`, `uint16`, `int32`, `uint32`,
`int64`, `uint64`, `float`, `double`.

## Worked example: *Survival Log* money

```
GameSaveData                       (74 members)
 └─[19] CurSave      : SaveChildData (194 members)
        └─[0] LeadingRole : AgentSave (148 members)
              ├─[38] Name  : string
              └─[39] Money : int      -> 0x1160 in this save
```

The run above is reproducible end to end; see [`docs/save-format.md`](docs/save-format.md)
for how the wire format was pinned down and how the offsets were verified.

## Limitations

- **Offsets are per-save-file.** MemoryPack is a variable-length format: as soon as the
  game writes a new save with a different number of items / NPCs / strings, every offset
  after that point moves. Re-run `find` after every new save. (`read` at a stale offset
  will show an obviously wrong value — treat that as a stop sign.)
- **The walk can drift on unusual members.** The reader covers the common cases
  (primitives, `string`, `List<T>`, `T[]`, `Dictionary<K,V>`, `Nullable<T>`, enums,
  nested `[MemoryPackable]` objects). Types with a hand-written
  `MemoryPackFormatter` are not modelled, and the walk stops with an explicit error
  when it hits one instead of silently producing garbage. Everything *before* that
  point is still correct — in the *Survival Log* save the drift starts at member 60 of
  `AgentSave`, while the target field is member 39.
- **Circular references / unions** (`GenerateType.CircularReference`, `Union`) are not
  implemented.

## Legal

This is an interoperability tool for save files you own, on your own machine. It ships
no game code, no decompiled output and no game assets. Whether you may use it is governed
by the game's EULA — check it, and prefer offline/single-player use. Do not use it in
multiplayer or to gain an unfair advantage.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文说明

`mp_reader.py` 用来在 **MemoryPack** 格式的存档里定位任意字段的文件偏移。

需要两样东西：Cpp2IL 导出的类结构（`--output-as diffable-cs`）和存档文件本身。
因为 MemoryPack 按字段声明顺序写入，而类结构里同时有顺序和类型，所以不用分析
游戏二进制也能把字节布局推出来。

```bash
# 列出某个类按序列化顺序排列的字段
python3 mp_reader.py --schema-dir out/DiffableCs members GameCore.HotUpdate.AgentSave

# 在存档里找到某个字段的偏移
python3 mp_reader.py --schema-dir out/DiffableCs find --save Save.bytes Money

# 读取 / 写入
python3 mp_reader.py read  --save Save.bytes 0x1160 --type int32
python3 mp_reader.py patch --save Save.bytes 0x1160 1000 --type int32
```

注意两点：**偏移只对当前这个存档有效**，游戏重新存档后需要重新 `find`；
`patch` 会自动生成 `*.bak-before-patch` 备份，但改之前请先完全退出游戏。

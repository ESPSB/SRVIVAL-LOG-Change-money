#!/usr/bin/env python3
"""Read and locate fields inside a MemoryPack-serialized save file.

This tool walks a save file produced by the Cysharp ``MemoryPack`` serializer
and reports the file offset of any field, using a type dump produced by
`Cpp2IL <https://github.com/SamboyCoding/Cpp2IL>`_ (``--output-as diffable-cs``).

It does *not* need the game binary at run time: the Cpp2IL text dump carries the
field order and the field types, and MemoryPack writes members in declaration
order, so the byte layout can be reconstructed from the dump alone.

Wire format implemented here (see ``docs/save-format.md``)::

    object          : 1 byte member count (0..249), 255 = null
    collection/array: int32 length, -1 = null
    string          : int32 ~utf8ByteCount, int32 utf16Length, utf8 bytes
                      0 = empty string, -1 = null
    bool 1B, int32 4B, int64 8B, float 4B, double 8B

Example::

    # 1. dump the game once (see README)
    # 2. locate a field
    python3 mp_reader.py --schema-dir out/DiffableCs \\
        find --save Save.bytes --root GameCore.HotUpdate.GameSaveData Money

Requires Python 3.8+. Standard library only.
"""

import argparse
import os
import re
import struct
import sys

# --------------------------------------------------------------------------- #
# Cpp2IL "diffable C#" dump parsing
# --------------------------------------------------------------------------- #

# A field line looks like:
#     private string <Name>k__BackingField; //Field offset: 0x10
#     public int InitChapterId; //Field offset: 0x24
FIELD_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal)\s+)?"
    r"(?:(?:static|readonly|const|volatile|new|unsafe|extern|fixed)\s+)*"
    r"([A-Za-z_][\w\.]*(?:<[^;=]*?>)?(?:\[\s*\])*)\s+"
    r"(<[^;=]+>k__BackingField|[A-Za-z_]\w*)\s*;\s*//Field offset:",
)

ENUM_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal)\s+)?"
    r"enum\s+([A-Za-z_]\w*)\s*(?::\s*([A-Za-z_]\w*))?"
)

DECL_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|sealed|abstract|static|partial|unsafe)\s+)*"
    r"(class|struct)\s+([A-Za-z_]\w*)"
)


def _parse_cs_file(path):
    """Return ``[(className, [(typeStr, fieldName), ...]), ...]`` for one dump file.

    The Cpp2IL layout is one top-level type per file, in a directory tree that
    mirrors the namespace. Nested types (e.g. the generated ``*Formatter``) are
    skipped by tracking brace depth.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []

    classes = []
    depth = 0
    pending = None
    current = None
    fields = []

    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if depth == 0 and pending is None and current is None:
            m = DECL_RE.match(line.strip())
            if m:
                pending = m.group(2)
        if current is not None and depth == 1:
            m = FIELD_RE.match(line)
            if m:
                fields.append((m.group(1), m.group(2)))
        depth += line.count("{") - line.count("}")
        if pending is not None and depth >= 1:
            current, fields = pending, []
            pending = None
        if depth == 0 and current is not None:
            classes.append((current, fields))
            current, fields = None, []
    return classes


# --------------------------------------------------------------------------- #
# Type system
# --------------------------------------------------------------------------- #

ALIAS = {
    "int": "i32", "Int32": "i32", "uint": "u32", "UInt32": "u32",
    "long": "i64", "Int64": "i64", "ulong": "u64", "UInt64": "u64",
    "short": "i16", "Int16": "i16", "ushort": "u16", "UInt16": "u16",
    "byte": "u8", "Byte": "u8", "sbyte": "i8", "SByte": "i8",
    "float": "f32", "Single": "f32", "double": "f64", "Double": "f64",
    "bool": "bool", "Boolean": "bool", "char": "char", "Char": "char",
    "string": "string", "String": "string",
}

PRIM_SIZE = {
    "i32": 4, "u32": 4, "i64": 8, "u64": 8, "i16": 2, "u16": 2,
    "u8": 1, "i8": 1, "f32": 4, "f64": 8, "bool": 1, "char": 2,
}

LIST_NAMES = {
    "List", "IList", "IReadOnlyList", "HashSet", "IEnumerable",
    "ICollection", "ObservableCollection", "Queue", "Stack",
}
DICT_NAMES = {"Dictionary", "IDictionary", "SortedDictionary"}


def split_args(text):
    """Split ``A, B`` respecting nested generics."""
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts]


class Schema:
    """Class/field/type information extracted from a Cpp2IL dump."""

    def __init__(self):
        self.members = {}   # full type name -> [(type str, field name), ...]
        self.namespace = {}  # full type name -> namespace
        self.by_simple = {}  # simple name -> {full names}
        self.enums = {}     # full enum name -> underlying C# type name
        self._cache = {}    # (kind, key) -> compiled value

    # -- loading ----------------------------------------------------------- #
    def load_dir(self, cs_root):
        for root, _dirs, files in os.walk(cs_root):
            for name in files:
                if not name.endswith(".cs"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, cs_root)
                parts = rel.split(os.sep)
                namespace = ".".join(parts[1:-1])
                self._load_file(path, namespace)
        return self

    def _load_file(self, path, namespace):
        for cls, fields in _parse_cs_file(path):
            full = (namespace + "." + cls) if namespace else cls
            if len(fields) > len(self.members.get(full, ())):
                self.members[full] = fields
            self.namespace[full] = namespace
            self.by_simple.setdefault(cls, set()).add(full)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return
        for line in text.split("\n"):
            m = ENUM_RE.match(line)
            if m:
                full = (namespace + "." + m.group(1)) if namespace else m.group(1)
                self.enums[full] = m.group(2) or "Int32"
                self.by_simple.setdefault(m.group(1), set()).add(full)

    # -- lookup ------------------------------------------------------------ #
    def resolve(self, name, context_ns):
        """Resolve a short type name to a full name using namespace context."""
        if name in self.members or name in self.enums:
            return name
        ns = context_ns
        while True:
            candidate = (ns + "." + name) if ns else name
            if candidate in self.members or candidate in self.enums:
                return candidate
            if not ns:
                break
            ns = ns.rsplit(".", 1)[0] if "." in ns else ""
        found = self.by_simple.get(name)
        return sorted(found)[0] if found else None

    def find_class(self, name):
        if name in self.members:
            return name
        found = self.by_simple.get(name)
        if not found:
            return None
        classes = sorted(f for f in found if f in self.members)
        return classes[0] if classes else None

    def parse_type(self, text, context_ns):
        key = ("t", text, context_ns)
        if key not in self._cache:
            self._cache[key] = self._parse_type(text.strip(), context_ns)
        return self._cache[key]

    def _parse_type(self, text, context_ns):
        if text.endswith("?") and len(text) > 1:
            inner = self._parse_type(text[:-1], context_ns)
            if inner[0] in PRIM_SIZE or inner[0] == "enum":
                return ("nullable", inner)
            return inner
        if text.endswith("[]"):
            return ("array", self._parse_type(text[:-2], context_ns))

        base = re.sub(r"`\d+$", "", text.split("<", 1)[0].strip())
        args = []
        if "<" in text and text.rstrip().endswith(">"):
            args = split_args(text[text.index("<") + 1: text.rindex(">")])
        short = base.split(".")[-1]

        if short in LIST_NAMES and args:
            return ("list", self._parse_type(args[0], context_ns))
        if short in DICT_NAMES and len(args) == 2:
            return ("dict",
                    self._parse_type(args[0], context_ns),
                    self._parse_type(args[1], context_ns))
        if short == "Nullable" and args:
            return ("nullable", self._parse_type(args[0], context_ns))
        if base in ALIAS:
            return (ALIAS[base],)

        full = self.resolve(base, context_ns)
        if full is None:
            return ("unknown", base)
        if full in self.enums:
            underlying = ALIAS.get(self.enums[full].split(".")[-1], "i32")
            return ("enum", underlying)
        return ("obj", full)

    def compiled_members(self, full):
        """``[(fieldName, typeNode), ...]`` resolved in the class' namespace."""
        key = ("cls", full)
        if key not in self._cache:
            ns = self.namespace.get(full, "")
            self._cache[key] = [
                (name, self.parse_type(type_str, ns))
                for type_str, name in self.members[full]
            ]
        return self._cache[key]


# --------------------------------------------------------------------------- #
# MemoryPack reader
# --------------------------------------------------------------------------- #

NULL_OBJECT = 255
MAX_COLLECTION = 5_000_000


class MemoryPackError(Exception):
    pass


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def u8(self):
        value = self.data[self.pos]
        self.pos += 1
        return value

    def raw(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise MemoryPackError(
                "need %d bytes at 0x%X but only %d remain"
                % (count, self.pos, len(self.data) - self.pos))
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def i32(self):
        return struct.unpack("<i", self.raw(4))[0]

    def scalar(self, kind):
        chunk = self.raw(PRIM_SIZE[kind])
        if kind == "bool":
            return chunk[0] != 0
        if kind == "f32":
            return struct.unpack("<f", chunk)[0]
        if kind == "f64":
            return struct.unpack("<d", chunk)[0]
        return int.from_bytes(chunk, "little", signed=kind.startswith("i"))

    def string(self):
        header = self.i32()
        if header == -1:
            return None
        if header == 0:
            return ""
        if header > 0:
            raise MemoryPackError(
                "string header is positive (%d) at 0x%X - misaligned?"
                % (header, self.pos - 4))
        byte_count = (~header) & 0xFFFFFFFF
        self.i32()  # utf16 length, not needed here
        chunk = self.raw(byte_count)
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            return "<%d bytes, invalid utf8>" % byte_count


class Walker:
    """Walks a MemoryPack object graph, calling hooks for objects and members."""

    def __init__(self, schema, data):
        self.schema = schema
        self.reader = Reader(data)
        self.objects = []
        self.members = []
        self.on_object = None   # (offset, depth, full_name, member_count) -> bool: recurse?
        self.on_member = None   # (offset, path, type_node, class_name, depth) -> None

    def read_value(self, node, path, depth):
        kind = node[0]
        if kind in PRIM_SIZE:
            return self.reader.scalar(kind)
        if kind == "string":
            return self.reader.string()
        if kind == "enum":
            return self.reader.scalar(node[1])
        if kind in ("array", "list"):
            count = self.reader.i32()
            if count == -1:
                return None
            if count < 0 or count > MAX_COLLECTION:
                raise MemoryPackError(
                    "implausible collection length %d at 0x%X (%s)"
                    % (count, self.reader.pos - 4, path))
            return [self.read_value(node[1], "%s[%d]" % (path, i), depth + 1)
                    for i in range(count)]
        if kind == "dict":
            count = self.reader.i32()
            if count == -1:
                return None
            if count < 0 or count > MAX_COLLECTION:
                raise MemoryPackError(
                    "implausible dictionary length %d at 0x%X (%s)"
                    % (count, self.reader.pos - 4, path))
            return [(self.read_value(node[1], path + ".key", depth + 1),
                     self.read_value(node[2], path + ".value", depth + 1))
                    for _ in range(count)]
        if kind == "nullable":
            if self.reader.u8() == 0:
                return None
            return self.read_value(node[1], path, depth + 1)
        if kind == "obj":
            return self.read_object(node[1], path, depth)
        raise MemoryPackError("unsupported type %r at %s (0x%X)"
                              % (node[1], path, self.reader.pos))

    def read_object(self, full_name, path, depth):
        offset = self.reader.pos
        header = self.reader.u8()
        if header == NULL_OBJECT:
            return None

        members = self.schema.compiled_members(full_name)
        if header > len(members):
            raise MemoryPackError(
                "member count %d exceeds schema (%d members) for %s at 0x%X "
                "- the walk has drifted" % (header, len(members), full_name, offset))

        recurse = True
        if self.on_object is not None:
            recurse = self.on_object(offset, depth, full_name, header)
        self.objects.append((offset, depth, full_name, header))

        for index in range(header):
            field_name, node = members[index]
            clean = field_name.replace("k__BackingField", "").strip("<>")
            child = path + "." + clean
            if self.on_member is not None:
                self.on_member(self.reader.pos, child, node, full_name, depth)
            if recurse:
                self.read_value(node, child, depth + 1)
            else:
                self.skip_value(node)
        return None

    def skip_value(self, node):
        """Advance past a value without building Python objects for it."""
        kind = node[0]
        if kind in PRIM_SIZE:
            self.reader.raw(PRIM_SIZE[kind])
        elif kind == "string":
            self.reader.string()
        elif kind == "enum":
            self.reader.raw(PRIM_SIZE[node[1]])
        elif kind in ("array", "list"):
            count = self.reader.i32()
            if count < 0:
                return
            for _ in range(count):
                self.skip_value(node[1])
        elif kind == "dict":
            count = self.reader.i32()
            if count < 0:
                return
            for _ in range(count):
                self.skip_value(node[1])
                self.skip_value(node[2])
        elif kind == "nullable":
            if self.reader.u8() != 0:
                self.skip_value(node[1])
        elif kind == "obj":
            self.skip_object(node[1])
        else:
            raise MemoryPackError("unsupported type %r at 0x%X" % (node[1], self.reader.pos))

    def skip_object(self, full_name):
        offset = self.reader.pos
        header = self.reader.u8()
        if header == NULL_OBJECT:
            return
        members = self.schema.compiled_members(full_name)
        if header > len(members):
            raise MemoryPackError(
                "member count %d exceeds schema (%d members) for %s at 0x%X"
                % (header, len(members), full_name, offset))
        self.objects.append((offset, 0, full_name, header))
        for index in range(header):
            self.skip_value(members[index][1])

    def walk(self, root):
        self.read_object(root, "$", 0)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_offset(text):
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def require_schema(args):
    if not args.schema_dir:
        sys.exit("error: --schema-dir is required for this command")
    if not os.path.isdir(args.schema_dir):
        sys.exit("error: --schema-dir is not a directory: %s" % args.schema_dir)
    return Schema().load_dir(args.schema_dir)


def cmd_classes(args, schema):
    pattern = re.compile(args.pattern) if args.pattern else None
    names = sorted(schema.members)
    hits = [n for n in names if pattern is None or pattern.search(n)]
    for name in hits:
        print("%-70s %d members" % (name, len(schema.members[name])))
    print("\n%d / %d classes" % (len(hits), len(names)))


def cmd_members(args, schema):
    full = schema.find_class(args.class_name)
    if full is None:
        sys.exit("class not found: %s" % args.class_name)
    if full != args.class_name:
        print("# resolved %s -> %s" % (args.class_name, full))
    for index, (type_str, name) in enumerate(schema.members[full]):
        print("[%3d] %-46s : %s"
              % (index, name.replace("k__BackingField", "").strip("<>"), type_str))


def cmd_walk(args, schema):
    data = open(args.save, "rb").read()
    root = schema.find_class(args.root)
    if root is None:
        sys.exit("class not found: %s" % args.root)

    walker = Walker(schema, data)
    walker.on_object = lambda off, d, name, n: d < args.max_depth

    def on_member(off, path, node, cls, depth):
        if depth <= args.max_depth + 1:
            walker.members.append((off, depth, path, node))

    walker.on_member = on_member

    status = "ok"
    try:
        walker.walk(root)
    except MemoryPackError as exc:
        status = str(exc)

    print("objects (first %d):" % args.limit)
    for off, depth, name, count in walker.objects[:args.limit]:
        print("  0x%06X  %s%s (members=%d)"
              % (off, "  " * min(depth, 20), name, count))
    print()
    print("members:")
    for off, depth, path, node in walker.members[:args.limit * 4]:
        print("  0x%06X  %s%-56s %s"
              % (off, "  " * min(depth, 8), path, node))
    print()
    print("stopped at 0x%X: %s" % (walker.reader.pos, status))


def cmd_find(args, schema):
    data = open(args.save, "rb").read()
    root = schema.find_class(args.root)
    if root is None:
        sys.exit("class not found: %s" % args.root)
    matcher = re.compile(args.pattern) if args.regex else re.compile(re.escape(args.pattern))

    hits = []
    walker = Walker(schema, data)
    walker.on_object = lambda off, d, name, n: True

    def on_member(off, path, node, cls, depth):
        leaf = path.rsplit(".", 1)[-1]
        if matcher.search(leaf):
            hits.append((off, path, node, cls))

    walker.on_member = on_member

    status = "ok"
    try:
        walker.walk(root)
    except MemoryPackError as exc:
        status = str(exc)

    for off, path, node, cls in hits:
        print("0x%06X  %-58s %-16s in %s" % (off, path, node, cls))
    print()
    print("%d hit(s); walk stopped at 0x%X: %s"
          % (len(hits), walker.reader.pos, status))


def cmd_read(args):
    data = open(args.save, "rb").read()
    offset = parse_offset(args.offset)
    kind = args.type
    as_int = {
        "int32": "<i", "int64": "<q", "uint32": "<I", "uint64": "<Q",
        "float": "<f", "double": "<d", "int16": "<h", "uint16": "<H",
    }
    if kind == "byte":
        print("0x%06X  u8  = %d" % (offset, data[offset]))
    elif kind in as_int:
        print("0x%06X  %-6s = %s"
              % (offset, kind, struct.unpack_from(as_int[kind], data, offset)[0]))
    else:
        sys.exit("unsupported --type %s" % kind)


def cmd_patch(args):
    offset = parse_offset(args.offset)
    kind = args.type
    as_int = {
        "int32": "<i", "int64": "<q", "uint32": "<I", "uint64": "<Q",
        "float": "<f", "double": "<d", "int16": "<h", "uint16": "<H",
    }
    if kind == "byte":
        blob = struct.pack("<B", int(args.value, 0) & 0xFF)
    elif kind in as_int:
        value = float(args.value) if kind in ("float", "double") else int(args.value, 0)
        blob = struct.pack(as_int[kind], value)
    else:
        sys.exit("unsupported --type %s" % kind)

    data = bytearray(open(args.save, "rb").read())
    before = struct.unpack_from(as_int.get(kind, "<B"), data, offset)[0] \
        if kind in as_int else data[offset]

    if not args.no_backup:
        backup = args.save + ".bak-before-patch"
        with open(backup, "wb") as fh:
            fh.write(data)
        print("backup written to %s" % backup)

    data[offset:offset + len(blob)] = blob
    with open(args.save, "wb") as fh:
        fh.write(bytes(data))
    print("0x%06X  %s: %s -> %s" % (offset, kind, before, args.value))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mp_reader.py",
        description="Locate and read fields inside a MemoryPack save file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Example::")[-1].strip())
    parser.add_argument("--schema-dir", metavar="DIR",
                        help="Cpp2IL 'diffable-cs' output directory "
                             "(the folder that contains the assembly folders)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("classes", help="list classes found in the dump")
    p.add_argument("pattern", nargs="?", help="optional regex filter")
    p.set_defaults(func=cmd_classes, needs_schema=True)

    p = sub.add_parser("members", help="list the members of one class, in serialized order")
    p.add_argument("class_name")
    p.set_defaults(func=cmd_members, needs_schema=True)

    p = sub.add_parser("walk", help="walk a save file and print the object tree")
    p.add_argument("--save", required=True)
    p.add_argument("--root", default="GameCore.HotUpdate.GameSaveData")
    p.add_argument("--max-depth", type=int, default=2,
                   help="do not descend past this object depth (default 2)")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_walk, needs_schema=True)

    p = sub.add_parser("find", help="walk a save file and print field offsets")
    p.add_argument("--save", required=True)
    p.add_argument("--root", default="GameCore.HotUpdate.GameSaveData")
    p.add_argument("pattern", help="field name (or regex with --regex)")
    p.add_argument("--regex", action="store_true")
    p.set_defaults(func=cmd_find, needs_schema=True)

    p = sub.add_parser("read", help="read a primitive at a file offset")
    p.add_argument("--save", required=True)
    p.add_argument("offset")
    p.add_argument("--type", default="int32")
    p.set_defaults(func=cmd_read, needs_schema=False)

    p = sub.add_parser("patch", help="write a primitive at a file offset (with backup)")
    p.add_argument("--save", required=True)
    p.add_argument("offset")
    p.add_argument("value")
    p.add_argument("--type", default="int32")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_patch, needs_schema=False)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    schema = require_schema(args) if args.needs_schema else None
    args.func(args, schema) if args.needs_schema else args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

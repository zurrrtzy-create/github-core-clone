"""Git object model, packfile read/write and pkt-line framing.

Pure standard library. Objects are stored byte-exactly: this module never
re-serialises an object it read, so a clone round-trips pushed bytes.
"""
import hashlib
import struct
import zlib

ZERO_OID = "0" * 40
_HEX40 = set("0123456789abcdef")


def is_oid(value):
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX40 for c in value)


def hash_object(obj_type, data):
    """SHA-1 over '<type> <size>\\0' + data, exactly as git computes it."""
    h = hashlib.sha1()
    h.update(("%s %d\0" % (obj_type, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------- commits

def parse_commit(data):
    """Return {tree, parents, author, committer, message} for commit bytes."""
    blank = data.find(b"\n\n")
    header = data if blank < 0 else data[:blank]
    message = b"" if blank < 0 else data[blank + 2:]
    tree = ""
    parents = []
    author = ""
    committer = ""
    for line in header.split(b"\n"):
        if not line:
            continue
        sp = line.find(b" ")
        key = line if sp < 0 else line[:sp]
        val = b"" if sp < 0 else line[sp + 1:]
        if key == b"tree":
            tree = val.decode("ascii")
        elif key == b"parent":
            parents.append(val.decode("ascii"))
        elif key == b"author":
            author = val.decode("utf-8", "replace")
        elif key == b"committer":
            committer = val.decode("utf-8", "replace")
    return {"tree": tree, "parents": parents, "author": author,
            "committer": committer, "message": message.decode("utf-8", "replace")}


def build_commit(tree, parents, message, author, timestamp=1700000000, timezone="+0000"):
    lines = ["tree %s" % tree]
    for p in parents:
        lines.append("parent %s" % p)
    lines.append("author %s %d %s" % (author, timestamp, timezone))
    lines.append("committer %s %d %s" % (author, timestamp, timezone))
    lines.append("")
    return ("\n".join(lines) + "\n" + message).encode("utf-8")


# ---------------------------------------------------------------- trees

def parse_tree(data):
    """Return [(mode, name, oid_hex)] in the object's stored order."""
    entries = []
    off = 0
    n = len(data)
    while off < n:
        sp = data.index(b" ", off)
        nul = data.index(b"\x00", sp + 1)
        mode = data[off:sp].decode("ascii")
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        off = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    key = (name + "/") if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries):
    """Build tree bytes from [(mode, name, oid_hex)], git-sorted."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += ("%s %s\0" % (mode, name)).encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def build_tree_from_files(files, put):
    """Build nested trees for {path: (mode, oid)}.

    put(type, data) stores an object and returns its oid. Returns the root
    tree oid. Files are blobs (mode 100644); directories become mode 40000.
    """
    # nested dict: name -> ('file', oid) | ('dir', {children})
    root = {}

    def insert(node, segments, mode, oid):
        head = segments[0]
        if len(segments) == 1:
            node[head] = ("file", mode, oid)
        else:
            child = node.get(head)
            if child is None or child[0] != "dir":
                child = ("dir", {})
                node[head] = child
            insert(child[1], segments[1:], mode, oid)

    for path, (mode, oid) in files.items():
        insert(root, path.split("/"), mode, oid)

    def build(node):
        entries = []
        for name, val in node.items():
            if val[0] == "file":
                entries.append((val[1], name, val[2]))
            else:
                entries.append(("40000", name, build(val[1])))
        return put("tree", build_tree(entries))

    return build(root)


def tree_lookup(objects, tree_oid, segments):
    """Oid of the blob/tree at segments under tree_oid, or None."""
    current = tree_oid
    for i, seg in enumerate(segments):
        obj = objects.get(current)
        if obj is None or obj[0] != "tree":
            return None
        found = None
        for mode, name, oid in parse_tree(obj[1]):
            if name == seg:
                found = (mode, oid)
                break
        if found is None:
            return None
        mode, oid = found
        if i == len(segments) - 1:
            return oid
        if mode != "40000":
            return None
        current = oid
    return None


def flatten_tree(objects, tree_oid, prefix=""):
    """{path: (mode, oid)} for every blob under tree_oid (recursive)."""
    out = {}
    obj = objects.get(tree_oid)
    if obj is None or obj[0] != "tree":
        return out
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == "40000":
            out.update(flatten_tree(objects, oid, path + "/"))
        else:
            out[path] = (mode, oid)
    return out


# ---------------------------------------------------------------- reachability

def collect_reachable(objects, roots, boundaries=None):
    """Every oid reachable from roots (commits, trees, tags, blobs).

    `boundaries` are commits the caller holds only shallowly (its
    shallow-clone boundary): the boundary commit itself and its tree are
    included, but its parents are NOT traversed, because the caller does
    not hold them. That is what a deepen fetch needs: the client's `have`
    is its shallow tip, and the parent below the boundary must be sent.
    """
    boundaries = boundaries or set()
    seen = set()
    queue = [r for r in roots if is_oid(r)]
    while queue:
        oid = queue.pop()
        if oid in seen:
            continue
        obj = objects.get(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype, data = obj
        if otype == "commit":
            c = parse_commit(data)
            queue.append(c["tree"])
            if oid not in boundaries:
                queue.extend(c["parents"])
        elif otype == "tag":
            # tag header: object <oid>
            for line in data.split(b"\n\n", 1)[0].split(b"\n"):
                if line.startswith(b"object "):
                    queue.append(line[7:].decode("ascii"))
        elif otype == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    queue.append(child)
    return seen


def depth_limited(objects, roots, depth):
    """Objects within `depth` commit generations of roots.

    Returns (oids, shallow_commits, unshallow_candidates): oids holds the
    commits within depth plus their trees/blobs; shallow_commits are the
    included commits whose parents are not included.
    """
    commits = set()
    frontier = [r for r in roots if is_oid(r)]
    for _ in range(depth):
        nxt = []
        for oid in frontier:
            if oid in commits:
                continue
            obj = objects.get(oid)
            if obj is None or obj[0] != "commit":
                continue
            commits.add(oid)
            nxt.extend(parse_commit(obj[1])["parents"])
        frontier = nxt
    out = set()
    for oid in commits:
        obj = objects.get(oid)
        if obj is None:
            continue
        out.add(oid)
        c = parse_commit(obj[1])
        out |= collect_reachable(objects, [c["tree"]])
    shallow = set()
    for oid in commits:
        c = parse_commit(objects[oid][1])
        if any(p not in commits for p in c["parents"]):
            shallow.add(oid)
    return out, shallow


# ---------------------------------------------------------------- packfiles

_TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


def _pack_entry_header(buf, off):
    b0 = buf[off]
    size = b0 & 0x0F
    shift = 4
    off += 1
    b = b0
    while b & 0x80:
        b = buf[off]
        off += 1
        size |= (b & 0x7F) << shift
        shift += 7
    type_code = (b0 >> 4) & 0x07
    return type_code, size, off


def _read_ofs_base(buf, off):
    b = buf[off]
    off += 1
    value = b & 0x7F
    while b & 0x80:
        b = buf[off]
        off += 1
        value = ((value + 1) << 7) | (b & 0x7F)
    return value, off


def _apply_delta(base, delta):
    off = 0

    def read_size():
        nonlocal off
        value = 0
        shift = 0
        while True:
            b = delta[off]
            off += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return value

    src_size = read_size()
    tgt_size = read_size()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray(tgt_size)
    written = 0
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[off] << shift
                    off += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    cp_size |= delta[off] << shift
                    off += 1
                shift += 8
            if cp_size == 0:
                cp_size = 0x10000
            out[written:written + cp_size] = base[cp_off:cp_off + cp_size]
            written += cp_size
        elif op:
            out[written:written + op] = delta[off:off + op]
            off += op
            written += op
        else:
            raise ValueError("delta opcode 0")
    if written != tgt_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def read_pack(data):
    """Parse a packfile; return [(oid, type, bytes)] in pack order.

    Resolves OFS_DELTA and REF_DELTA (thin-pack bases must be in the pack).
    Raises ValueError on a corrupt pack.
    """
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = struct.unpack(">I", data[4:8])[0]
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = struct.unpack(">I", data[8:12])[0]
    trailer = data[len(data) - 20:].hex()
    if hashlib.sha1(data[:len(data) - 20]).hexdigest() != trailer:
        raise ValueError("pack trailer checksum mismatch")
    end = len(data) - 20
    off = 12
    raw = []  # (offset, type_code, size, data, base_offset|base_oid)
    by_offset = {}
    for _ in range(count):
        entry_off = off
        type_code, size, off = _pack_entry_header(data, off)
        base_off = None
        base_oid = None
        if type_code == 6:
            dist, off = _read_ofs_base(data, off)
            base_off = entry_off - dist
        elif type_code == 7:
            base_oid = data[off:off + 20].hex()
            off += 20
        elif type_code not in _CODE_TYPE:
            raise ValueError("unknown pack type %d" % type_code)
        dec = zlib.decompressobj()
        chunk = dec.decompress(data[off:end])
        consumed = len(data[off:end]) - len(dec.unused_data)
        off += consumed
        if len(chunk) != size:
            raise ValueError("pack entry size mismatch")
        raw.append((entry_off, type_code, size, chunk, base_off, base_oid))
        by_offset[entry_off] = len(raw) - 1
    if off != end:
        raise ValueError("pack trailing bytes")
    resolved = [None] * len(raw)
    by_oid = {}

    def resolve(i):
        if resolved[i] is not None:
            return resolved[i]
        _off, tc, _sz, chunk, base_off, base_oid = raw[i]
        if tc == 6:
            base = resolve(by_offset[base_off])
            otype, bdata = base[1], base[2]
            data = _apply_delta(bdata, chunk)
        elif tc == 7:
            base = by_oid.get(base_oid)
            if base is None:
                raise ValueError("ref-delta base %s not in pack" % base_oid)
            otype, bdata = base[1], base[2]
            data = _apply_delta(bdata, chunk)
        else:
            otype = _CODE_TYPE[tc]
            data = chunk
        oid = hash_object(otype, data)
        item = (oid, otype, data)
        resolved[i] = item
        by_oid[oid] = item
        return item

    return [resolve(i) for i in range(len(raw))]


def _pack_object_header(obj_type, size):
    code = _TYPE_CODE[obj_type]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects):
    """Build a packfile (version 2, no deltas) from [(type, bytes)].

    Objects are emitted sorted by oid, so the bytes are deterministic.
    """
    items = sorted(objects, key=lambda o: hash_object(o[0], o[1]))
    parts = [b"PACK" + struct.pack(">II", 2, len(items))]
    for otype, data in items:
        parts.append(_pack_object_header(otype, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


# ---------------------------------------------------------------- pkt-lines

def pkt_line(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


FLUSH = b"0000"
DELIM = b"0001"
RESPONSE_END = b"0002"


def pkt_lines(items):
    out = bytearray()
    for item in items:
        if item == "flush":
            out += FLUSH
        elif item == "delim":
            out += DELIM
        elif item == "response-end":
            out += RESPONSE_END
        else:
            out += pkt_line(item)
    return bytes(out)


def decode_pkt_lines(data):
    """Decode a pkt-line stream; return [(kind, payload)] where kind is
    'data', 'flush', 'delim' or 'response-end'. Stops at the first raw
    (non-framed) byte run and returns it as ('raw', bytes)."""
    packets = []
    off = 0
    n = len(data)
    while off < n:
        if n - off < 4:
            packets.append(("raw", data[off:]))
            break
        head = data[off:off + 4]
        try:
            length = int(head.decode("ascii"), 16)
        except ValueError:
            packets.append(("raw", data[off:]))
            break
        if length == 0:
            packets.append(("flush", b""))
            off += 4
        elif length == 1:
            packets.append(("delim", b""))
            off += 4
        elif length == 2:
            packets.append(("response-end", b""))
            off += 4
        elif length < 4 or off + length > n:
            packets.append(("raw", data[off:]))
            break
        else:
            packets.append(("data", data[off + 4:off + length]))
            off += length
    return packets

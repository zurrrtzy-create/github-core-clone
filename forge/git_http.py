"""Git smart-HTTP: info/refs advertisements, v2/v0 fetch, v0 receive-pack.
"""
from . import gitobj
from .store import ZERO

AGENT = "github-forge/1.0"

_V0_UPLOAD_CAPS = ("side-band-64k shallow ofs-delta thin-pack multi_ack "
                   "symref=HEAD:refs/heads/main agent=" + AGENT)
_V0_RECEIVE_CAPS = ("report-status report-status-v2 side-band-64k "
                   "delete-refs ofs-delta agent=" + AGENT)

_MAX_PKT_PAYLOAD = 65520 - 4


def _chunk_sideband(pack):
    """Split pack bytes into sideband band-1 pkt-lines (<=65520 each)."""
    out = bytearray()
    off = 0
    while off < len(pack):
        chunk = pack[off:off + _MAX_PKT_PAYLOAD - 1]
        out += gitobj.pkt_line(b"\x01" + chunk)
        off += len(chunk)
    if len(pack) == 0:
        out += gitobj.pkt_line(b"\x01")
    return bytes(out)


def _peel_target(repo, oid):
    """The commit an annotated tag points at, else None."""
    obj = repo.objects.get(oid)
    if obj is None or obj[0] != "tag":
        return None
    for line in obj[1].split(b"\n\n", 1)[0].split(b"\n"):
        if line.startswith(b"object "):
            target = line[7:].decode("ascii")
            tobj = repo.objects.get(target)
            if tobj is not None and tobj[0] == "commit":
                return target
    return None


def _sorted_refs(repo):
    heads = sorted(r for r in repo.refs if r.startswith("refs/heads/"))
    tags = sorted(r for r in repo.refs if r.startswith("refs/tags/"))
    other = sorted(r for r in repo.refs
                   if not r.startswith("refs/heads/")
                   and not r.startswith("refs/tags/"))
    return heads + tags + other


def advertisement(repo, service, version):
    """(content_type, body) for GET info/refs."""
    ct = "application/x-%s-advertisement" % service
    if service == "git-upload-pack" and version == 2:
        lines = ["version 2\n", "ls-refs=unborn\n",
                 "fetch=shallow wait-for-done\n", "object-format=sha1\n",
                 "agent=%s\n" % AGENT, "flush"]
        return ct, gitobj.pkt_lines(lines)
    # v0
    lines = ["# service=%s\n" % service, "flush"]
    refs = _sorted_refs(repo)
    caps = _V0_UPLOAD_CAPS if service == "git-upload-pack" else _V0_RECEIVE_CAPS
    if not refs:
        lines.append("%s capabilities^{}\0%s\n" % (ZERO, caps))
    else:
        tip = repo.branch_tip(repo.default_branch)
        first = True
        if tip:
            line = "%s HEAD" % tip
            if first:
                line += "\0" + caps
                first = False
            lines.append(line + "\n")
        for ref in refs:
            oid = repo.refs[ref]
            line = "%s %s" % (oid, ref)
            if first:
                line += "\0" + caps
                first = False
            lines.append(line + "\n")
            peeled = _peel_target(repo, oid)
            if peeled:
                lines.append("%s %s^{}\n" % (peeled, ref))
    lines.append("flush")
    return ct, gitobj.pkt_lines(lines)


def ls_refs(repo, prefixes, peel, symrefs, unborn):
    """v2 ls-refs response body."""
    lines = []
    tip = repo.branch_tip(repo.default_branch)
    has_prefix = len(prefixes) > 0

    def keep(name):
        return not has_prefix or any(name.startswith(p) for p in prefixes)

    if tip is None:
        if unborn and keep("HEAD"):
            lines.append("unborn HEAD symref-target:%s\n"
                         % repo.head_symref())
    else:
        if keep("HEAD"):
            lines.append("%s HEAD symref-target:%s\n"
                         % (tip, repo.head_symref()))
        for ref in _sorted_refs(repo):
            if not keep(ref):
                continue
            oid = repo.refs[ref]
            line = "%s %s" % (oid, ref)
            if peel:
                peeled = _peel_target(repo, oid)
                if peeled:
                    line += " peeled:%s" % peeled
            lines.append(line + "\n")
    lines.append("flush")
    return gitobj.pkt_lines(lines)


def _fetch_objects(repo, wants, haves, depth, shallow_lines=()):
    """(send_oids, shallow, unshallow, have_closure)."""
    # The client's haves are held only up to its shallow boundaries:
    # a have that is itself a boundary contributes just itself and its
    # tree, not its parents (those are what a deepen fetch must deliver).
    have_closure = gitobj.collect_reachable(
        repo.objects, haves, set(shallow_lines))
    if depth is not None:
        send, shallow = gitobj.depth_limited(repo.objects, wants, depth)
    else:
        send = gitobj.collect_reachable(repo.objects, wants)
        shallow = set()
    send -= have_closure
    return send, shallow, have_closure


def _pack_of(repo, oids):
    objs = [(repo.objects[o][0], repo.objects[o][1]) for o in sorted(oids)
            if o in repo.objects]
    return gitobj.write_pack(objs)


def fetch_v2(repo, wants, haves, shallow_lines, depth, done):
    """v2 fetch response.

    Real git servers omit the acknowledgments section entirely when the
    client sends `done` in the same request (the only flow the kit and
    a fresh clone use): the response is [shallow-info delim-pkt] packfile
    flush-pkt. Sending an acknowledgments section first makes real git
    die with "expected 'packfile', received 'acknowledgments'".

    When the client has NOT sent `done` (a pull/fetch with haves does
    this in round 1), it is waiting for the acknowledgments: answer with
    the acknowledgments section + flush only, and the client re-sends
    with `done` to collect the packfile.
    """
    if not done:
        # Negotiation round: the client sent haves and is waiting for the
        # acknowledgments. If the server has every have, it answers with
        # ACKs + "ready" and the packfile follows IN THE SAME response
        # (real git dies with "expected packfile to be sent after 'ready'"
        # if the response ends after the acks). If some have is unknown,
        # answer with the ACKs known and no "ready", ending the response
        # there; the client then re-sends with `done`.
        acked = [h for h in haves if h in repo.objects]
        if acked and len(acked) == len(haves):
            out = bytearray()
            out += gitobj.pkt_line("acknowledgments\n")
            for h in acked:
                out += gitobj.pkt_line("ACK %s\n" % h)
            out += gitobj.pkt_line("ready\n")
            out += gitobj.DELIM
            send, shallow, have_closure = _fetch_objects(
                repo, wants, haves, depth, shallow_lines)
            pack = _pack_of(repo, send)
            unshallow = set()
            if depth is not None:
                for old in shallow_lines:
                    obj = repo.objects.get(old)
                    if obj is not None and obj[0] == "commit":
                        parents = gitobj.parse_commit(obj[1])["parents"]
                        if any(p in send or p in have_closure
                               for p in parents):
                            unshallow.add(old)
                out += gitobj.pkt_line("shallow-info\n")
                for oid in sorted(shallow):
                    out += gitobj.pkt_line("shallow %s\n" % oid)
                for oid in sorted(unshallow):
                    out += gitobj.pkt_line("unshallow %s\n" % oid)
                out += gitobj.DELIM
            out += gitobj.pkt_line("packfile\n")
            out += _chunk_sideband(pack)
            out += gitobj.FLUSH
            return bytes(out)
        out = bytearray()
        out += gitobj.pkt_line("acknowledgments\n")
        for h in acked:
            out += gitobj.pkt_line("ACK %s\n" % h)
        if not acked:
            out += gitobj.pkt_line("NAK\n")
        out += gitobj.FLUSH
        return bytes(out)
    send, shallow, have_closure = _fetch_objects(
        repo, wants, haves, depth, shallow_lines)
    pack = _pack_of(repo, send)
    out = bytearray()
    unshallow = set()
    if depth is not None:
        for old in shallow_lines:
            if old in have_closure or old in send:
                # the old boundary's parent is now present when the old
                # boundary itself is in the send set's ancestry... an old
                # boundary stops being shallow once its parent is delivered
                # or already held.
                obj = repo.objects.get(old)
                if obj is not None and obj[0] == "commit":
                    parents = gitobj.parse_commit(obj[1])["parents"]
                    if any(p in send or p in have_closure for p in parents):
                        unshallow.add(old)
        out += gitobj.pkt_line("shallow-info\n")
        for oid in sorted(shallow):
            out += gitobj.pkt_line("shallow %s\n" % oid)
        for oid in sorted(unshallow):
            out += gitobj.pkt_line("unshallow %s\n" % oid)
        out += gitobj.DELIM
    out += gitobj.pkt_line("packfile\n")
    out += _chunk_sideband(pack)
    out += gitobj.FLUSH
    return bytes(out)


def fetch_v0(repo, wants, haves, shallow_lines, depth):
    send, shallow, have_closure = _fetch_objects(
        repo, wants, haves, depth, shallow_lines)
    pack = _pack_of(repo, send)
    out = bytearray()
    if haves:
        for h in haves:
            out += gitobj.pkt_line("ACK %s\n" % h)
    else:
        out += gitobj.pkt_line("NAK\n")
    if depth is not None:
        for oid in sorted(shallow):
            out += gitobj.pkt_line("shallow %s\n" % oid)
        for old in shallow_lines:
            obj = repo.objects.get(old)
            if obj is not None and obj[0] == "commit":
                parents = gitobj.parse_commit(obj[1])["parents"]
                if any(p in send or p in have_closure for p in parents):
                    out += gitobj.pkt_line("unshallow %s\n" % old)
    out += _chunk_sideband(pack)
    out += gitobj.FLUSH
    return bytes(out)


def parse_fetch_request_v2(packets):
    """Parse a v2 fetch request body; returns a dict or None on garbage."""
    wants, haves, shallow = [], [], []
    depth = None
    done = False
    command = None
    in_args = False
    for kind, payload in packets:
        if kind == "raw":
            return None
        if kind in ("flush", "delim"):
            if kind == "delim":
                in_args = True
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace")
        if not in_args:
            if line.startswith("command="):
                command = line[len("command="):].strip()
            continue
        text = line.rstrip("\n")
        if text.startswith("want "):
            wants.append(text[5:].strip())
        elif text.startswith("have "):
            haves.append(text[5:].strip())
        elif text.startswith("shallow "):
            shallow.append(text[8:].strip())
        elif text.startswith("deepen "):
            try:
                depth = int(text[7:].strip())
            except ValueError:
                return None
        elif text == "done":
            done = True
    if command not in ("fetch", "ls-refs"):
        return None
    return {"command": command, "wants": wants, "haves": haves,
            "shallow": shallow, "depth": depth, "done": done}


def parse_fetch_request_v0(packets):
    """Parse a v0 fetch request; returns a dict or None on garbage."""
    wants, haves, shallow = [], [], []
    depth = None
    section = 0
    for kind, payload in packets:
        if kind == "raw":
            return None
        if kind == "flush":
            section += 1
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            first = line[5:].split(" ")
            if gitobj.is_oid(first[0]):
                wants.append(first[0])
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                return None
        elif line == "done":
            pass
        else:
            return None
    return {"command": "fetch", "wants": wants, "haves": haves,
            "shallow": shallow, "depth": depth}


def parse_ls_refs_request(packets):
    """Parse a v2 ls-refs request; returns (prefixes, peel, symrefs, unborn)."""
    prefixes, peel, symrefs, unborn = [], False, False, False
    in_args = False
    for kind, payload in packets:
        if kind == "raw":
            return None
        if kind == "delim":
            in_args = True
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if not in_args:
            continue
        if line == "peel":
            peel = True
        elif line == "symrefs":
            symrefs = True
        elif line == "unborn":
            unborn = True
        elif line.startswith("ref-prefix "):
            prefixes.append(line[len("ref-prefix "):])
    return prefixes, peel, symrefs, unborn


def parse_push_request(data):
    """Parse a receive-pack body; returns (commands, pack_bytes) or None."""
    packets = gitobj.decode_pkt_lines(data)
    commands = []
    pack = b""
    offset = 0
    for kind, payload in packets:
        if kind == "raw":
            pack = payload
            break
        if kind == "flush":
            offset += 4
            pack = data[offset:]
            break
        if kind == "delim":
            offset += 4
            continue
        if kind != "data":
            continue
        offset += 4 + len(payload)
        line = payload.decode("utf-8", "replace")
        nul = line.find("\x00")
        if nul >= 0:
            line = line[:nul]
        line = line.rstrip("\n")
        parts = line.split(" ")
        if len(parts) != 3:
            return None
        old, new, ref = parts
        if not gitobj.is_oid(old) or not gitobj.is_oid(new):
            return None
        commands.append((old, new, ref))
    return commands, pack


def apply_push(repo, store, commands, pack_bytes):
    """Store the pack, apply the commands; returns the report lines."""
    if pack_bytes:
        try:
            objects = gitobj.read_pack(pack_bytes)
        except ValueError as err:
            return ["unpack error: %s" % err], []
        store.add_pack(repo, objects)
    lines = ["unpack ok"]
    results = []
    with store.lock:
        for old, new, ref in commands:
            current = repo.refs.get(ref)
            if new == ZERO:
                if current is None:
                    lines.append("ng %s remote ref does not exist" % ref)
                    results.append((ref, False, "remote ref does not exist"))
                elif current != old:
                    lines.append("ng %s non-fast-forward" % ref)
                    results.append((ref, False, "non-fast-forward"))
                else:
                    del repo.refs[ref]
                    lines.append("ok %s" % ref)
                    results.append((ref, True, ""))
            elif old == ZERO:
                if current is not None:
                    lines.append("ng %s already exists" % ref)
                    results.append((ref, False, "already exists"))
                elif new not in repo.objects:
                    lines.append("ng %s unable to parse object" % ref)
                    results.append((ref, False, "unable to parse object"))
                else:
                    repo.refs[ref] = new
                    lines.append("ok %s" % ref)
                    results.append((ref, True, ""))
            else:
                if current is None:
                    lines.append("ng %s remote ref does not exist" % ref)
                    results.append((ref, False, "remote ref does not exist"))
                elif current != old:
                    lines.append("ng %s non-fast-forward" % ref)
                    results.append((ref, False, "non-fast-forward"))
                elif new not in repo.objects:
                    lines.append("ng %s unable to parse object" % ref)
                    results.append((ref, False, "unable to parse object"))
                elif not store.is_ancestor(repo, old, new):
                    lines.append("ng %s non-fast-forward" % ref)
                    results.append((ref, False, "non-fast-forward"))
                else:
                    repo.refs[ref] = new
                    lines.append("ok %s" % ref)
                    results.append((ref, True, ""))
    return lines, results


def push_report_body(lines):
    """The report-status stream, pkt-lined inside sideband band 1.

    The terminating flush packet belongs INSIDE the band-1 data (real
    git's receive-pack sends it there): the report reader reads the
    demuxed band-1 stream and expects a flush to end it; a bare outer
    flush is consumed by the sideband layer and leaves the reader
    staring at EOF ("the remote end hung up unexpectedly").
    """
    stream = b"".join(gitobj.pkt_line(line + "\n") for line in lines)
    stream += gitobj.FLUSH
    out = bytearray()
    off = 0
    while off < len(stream):
        chunk = stream[off:off + _MAX_PKT_PAYLOAD - 1]
        out += gitobj.pkt_line(b"\x01" + chunk)
        off += len(chunk)
    out += gitobj.FLUSH
    return bytes(out)

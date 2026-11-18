"""The forge's in-memory store: repositories, git objects, issues, pulls.

Everything is plain dicts guarded by one re-entrant lock for mutations;
read paths are lock-free (the suite never mutates concurrently with a
read except for concurrent read-only clones).
"""
import base64
import difflib
import threading

from . import gitobj
from .scenario import VIEWER_LOGIN, VIEWER_ID, VIEWER_NAME, VIEWER_EMAIL

FIXED_TS = "2025-01-01T00:00:00Z"
COMMIT_AUTHOR = "%s <%s>" % (VIEWER_NAME, VIEWER_EMAIL)
COMMIT_TS = 1700000000

ZERO = gitobj.ZERO_OID


def _b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _unb64(text):
    try:
        return base64.urlsafe_b64decode(text.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def repo_global_id(dbid):
    return _b64("Repository:%d" % dbid)


def issue_global_id(repo_id, number):
    return _b64("Issue:%d:%d" % (repo_id, number))


def pr_global_id(repo_id, number):
    return _b64("PullRequest:%d:%d" % (repo_id, number))


def comment_global_id(repo_id, issue_number, comment_id):
    return _b64("IssueComment:%d:%d:%d" % (repo_id, issue_number, comment_id))


def parse_global_id(gid):
    """(kind, [int parts]) or (None, []) for an opaque id we did not make."""
    text = _unb64(gid) if isinstance(gid, str) else None
    if not text:
        return None, []
    parts = text.split(":")
    kind = parts[0]
    if kind not in ("Repository", "Issue", "PullRequest", "IssueComment"):
        return None, []
    try:
        nums = [int(p) for p in parts[1:]]
    except ValueError:
        return None, []
    return kind, nums


class Repo:
    def __init__(self, dbid, name, owner, private, description):
        self.id = dbid
        self.name = name
        self.owner = owner
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = "main"
        self.objects = {}  # oid -> (type, bytes)
        self.refs = {}  # refname -> oid
        self.labels = {}  # name -> {name, color, description}
        self.issues = {}  # number -> dict
        self.pulls = {}  # number -> dict
        self.next_issue = 1
        self.next_pr = 1
        self.seq = 0  # creation order

    # -- git ------------------------------------------------------------
    def put_object(self, obj_type, data):
        oid = gitobj.hash_object(obj_type, data)
        if oid not in self.objects:
            self.objects[oid] = (obj_type, data)
        return oid

    def ref_tip(self, refname):
        return self.refs.get(refname)

    def branch_tip(self, branch):
        return self.refs.get("refs/heads/" + branch)

    def branches(self):
        out = []
        for ref, oid in self.refs.items():
            if ref.startswith("refs/heads/"):
                out.append((ref[len("refs/heads/"):], oid))
        out.sort()
        return out

    def head_symref(self):
        return "refs/heads/" + self.default_branch


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}  # (owner, name) -> Repo
        self.repos_by_id = {}  # dbid -> Repo
        self.next_repo_id = 5000
        self.next_object_id = 1  # REST numeric ids for issues/comments
        self.seq = 0

    # -- repositories ---------------------------------------------------
    def create_repo(self, name, private=False, description=""):
        with self.lock:
            key = (VIEWER_LOGIN, name)
            if key in self.repos:
                return None
            repo = Repo(self.next_repo_id, name, VIEWER_LOGIN, private, description)
            self.next_repo_id += 1
            self.seq += 1
            repo.seq = self.seq
            self.repos[key] = repo
            self.repos_by_id[repo.id] = repo
            return repo

    def get_repo(self, owner, name):
        if owner != VIEWER_LOGIN:
            return None
        return self.repos.get((owner, name))

    def get_repo_by_id(self, dbid):
        return self.repos_by_id.get(dbid)

    def delete_repo(self, owner, name):
        with self.lock:
            repo = self.get_repo(owner, name)
            if repo is None:
                return False
            del self.repos[(owner, name)]
            del self.repos_by_id[repo.id]
            return True

    def list_repos(self):
        return sorted(self.repos.values(), key=lambda r: r.seq)

    # -- git objects / refs ----------------------------------------------
    def add_pack(self, repo, objects):
        """Store [(oid, type, data)] from a parsed packfile."""
        with self.lock:
            for oid, otype, data in objects:
                if oid not in repo.objects:
                    repo.objects[oid] = (otype, data)

    def is_ancestor(self, repo, ancestor, descendant):
        """True when `ancestor` is reachable from `descendant` (or equal)."""
        if ancestor == descendant:
            return True
        seen = set()
        queue = [descendant]
        while queue:
            oid = queue.pop()
            if oid in seen:
                continue
            seen.add(oid)
            obj = repo.objects.get(oid)
            if obj is None or obj[0] != "commit":
                continue
            parents = gitobj.parse_commit(obj[1])["parents"]
            if ancestor in parents:
                return True
            queue.extend(parents)
        return False

    # -- contents / commits ----------------------------------------------
    def commit_files(self, repo, branch, changes, message):
        """Commit {path: bytes|None} on branch; returns (commit_oid, created).

        changes map a path to new bytes, or None to delete it. A missing
        branch is created from the default branch tip (or as a root commit).
        """
        with self.lock:
            tip = repo.branch_tip(branch)
            if tip is None and branch != repo.default_branch:
                base_tip = repo.branch_tip(repo.default_branch)
                if base_tip is not None:
                    repo.refs["refs/heads/" + branch] = base_tip
                    tip = base_tip
            if tip is None:
                tree_files = {}
                parents = []
            else:
                tree = gitobj.parse_commit(repo.objects[tip][1])["tree"]
                tree_files = gitobj.flatten_tree(repo.objects, tree)
                parents = [tip]
            new_files = dict(tree_files)
            for path, data in changes.items():
                if data is None:
                    new_files.pop(path, None)
                else:
                    blob = repo.put_object("blob", data)
                    new_files[path] = ("100644", blob)
            root = gitobj.build_tree_from_files(new_files, repo.put_object)
            commit = gitobj.build_commit(root, parents, message, COMMIT_AUTHOR, COMMIT_TS)
            commit_oid = repo.put_object("commit", commit)
            repo.refs["refs/heads/" + branch] = commit_oid
            return commit_oid, tip is None

    def read_path(self, repo, branch, path):
        """(kind, value) for path on branch: kind 'file' -> (oid, bytes),
        'dir' -> [(mode, name, oid)], None when missing."""
        tip = repo.branch_tip(branch)
        if tip is None:
            return None
        tree = gitobj.parse_commit(repo.objects[tip][1])["tree"]
        if path == "" or path is None:
            entries = gitobj.parse_tree(repo.objects[tree][1])
            return ("dir", [(m, n, o) for m, n, o in entries])
        segments = [s for s in path.split("/") if s not in ("", ".")]
        oid = gitobj.tree_lookup(repo.objects, tree, segments)
        if oid is None:
            return None
        obj = repo.objects.get(oid)
        if obj is None:
            return None
        if obj[0] == "tree":
            return ("dir", [(m, n, o) for m, n, o in gitobj.parse_tree(obj[1])])
        return ("file", (oid, obj[1]))

    def list_commits(self, repo, branch, limit=100):
        tip = repo.branch_tip(branch)
        out = []
        seen = set()
        queue = [tip] if tip else []
        while queue and len(out) < limit:
            oid = queue.pop(0)
            if oid in seen or oid is None:
                continue
            seen.add(oid)
            obj = repo.objects.get(oid)
            if obj is None or obj[0] != "commit":
                continue
            c = gitobj.parse_commit(obj[1])
            out.append({"sha": oid, "message": c["message"],
                        "author": c["author"], "committer": c["committer"]})
            queue.extend(c["parents"])
        return out

    # -- labels -----------------------------------------------------------
    def create_label(self, repo, name, color="ededed", description=""):
        with self.lock:
            if name in repo.labels:
                return repo.labels[name]
            label = {"name": name, "color": color, "description": description or ""}
            repo.labels[name] = label
            return label

    # -- issues -----------------------------------------------------------
    def create_issue(self, repo, title, body="", labels=None):
        with self.lock:
            number = repo.next_issue
            repo.next_issue += 1
            oid = self.next_object_id
            self.next_object_id += 1
            issue = {"id": oid, "number": number, "title": title,
                     "body": body or "", "state": "open",
                     "labels": list(labels or []), "comments": [],
                     "created_at": FIXED_TS, "updated_at": FIXED_TS,
                     "closed_at": None}
            repo.issues[number] = issue
            return issue

    def list_issues(self, repo, state="open", direction="desc"):
        issues = [i for i in repo.issues.values()
                  if state == "all" or i["state"] == state]
        issues.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
        return issues

    # -- pull requests ------------------------------------------------------
    def create_pr(self, repo, title, body, head_ref, base_ref):
        with self.lock:
            head_tip = repo.branch_tip(head_ref)
            base_tip = repo.branch_tip(base_ref)
            if head_tip is None or base_tip is None:
                return None, "missing branch"
            for pr in repo.pulls.values():
                if pr["state"] == "open" and pr["head_ref"] == head_ref \
                        and pr["base_ref"] == base_ref:
                    return None, "exists"
            number = repo.next_pr
            repo.next_pr += 1
            oid = self.next_object_id
            self.next_object_id += 1
            pr = {"id": oid, "number": number, "title": title,
                  "body": body or "", "state": "open", "merged": False,
                  "merged_at": None, "merge_commit_sha": None,
                  "head_ref": head_ref, "base_ref": base_ref,
                  "base_sha": base_tip, "created_at": FIXED_TS,
                  "updated_at": FIXED_TS, "closed_at": None}
            repo.pulls[number] = pr
            return pr, None

    def pr_diff(self, repo, pr):
        """[(path, status, additions, deletions, changes)] base->head."""
        base_tip = pr["base_sha"]
        head_tip = repo.branch_tip(pr["head_ref"])
        if head_tip is None:
            return []
        base_tree = gitobj.parse_commit(repo.objects[base_tip][1])["tree"]
        head_tree = gitobj.parse_commit(repo.objects[head_tip][1])["tree"]
        base_files = gitobj.flatten_tree(repo.objects, base_tree)
        head_files = gitobj.flatten_tree(repo.objects, head_tree)
        out = []
        for path in sorted(set(base_files) | set(head_files)):
            b = base_files.get(path)
            h = head_files.get(path)
            if b is None:
                data = repo.objects[h[1]][1]
                lines = len(data.splitlines())
                out.append((path, "added", lines, 0, lines))
            elif h is None:
                data = repo.objects[b[1]][1]
                lines = len(data.splitlines())
                out.append((path, "removed", 0, lines, lines))
            elif b[1] != h[1]:
                old = repo.objects[b[1]][1].decode("utf-8", "replace").splitlines()
                new = repo.objects[h[1]][1].decode("utf-8", "replace").splitlines()
                sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
                add = dele = 0
                for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
                    if tag in ("insert", "replace"):
                        add += j2 - j1
                    if tag in ("delete", "replace"):
                        dele += _i2 - _i1
                out.append((path, "modified", add, dele, add + dele))
        return out

    def pr_conflict(self, repo, pr):
        """True when the squash merge would conflict with the base tip."""
        base_tip = pr["base_sha"]
        head_tip = repo.branch_tip(pr["head_ref"])
        base_branch_tip = repo.branch_tip(pr["base_ref"])
        if head_tip is None or base_branch_tip is None:
            return True
        base_tree = gitobj.parse_commit(repo.objects[base_tip][1])["tree"]
        head_tree = gitobj.parse_commit(repo.objects[head_tip][1])["tree"]
        tip_tree = gitobj.parse_commit(repo.objects[base_branch_tip][1])["tree"]
        base_files = gitobj.flatten_tree(repo.objects, base_tree)
        head_files = gitobj.flatten_tree(repo.objects, head_tree)
        tip_files = gitobj.flatten_tree(repo.objects, tip_tree)
        for path in set(base_files) | set(head_files):
            b = base_files.get(path)
            h = head_files.get(path)
            t = tip_files.get(path)
            b_oid = b[1] if b else None
            h_oid = h[1] if h else None
            t_oid = t[1] if t else None
            if b_oid == h_oid:
                continue
            if t_oid == b_oid:
                continue
            if t_oid == h_oid:
                continue
            return True
        return False

    def pr_mergeable(self, repo, pr):
        if pr["state"] != "open":
            return "UNKNOWN"
        return "CONFLICTING" if self.pr_conflict(repo, pr) else "MERGEABLE"

    def merge_pr(self, repo, pr, method="squash"):
        """Squash-merge; returns (commit_oid, error) with error None on ok."""
        with self.lock:
            if pr["state"] != "open":
                return None, "not open"
            if self.pr_conflict(repo, pr):
                return None, "conflict"
            base_branch_tip = repo.branch_tip(pr["base_ref"])
            head_tip = repo.branch_tip(pr["head_ref"])
            base_tree = gitobj.parse_commit(repo.objects[pr["base_sha"]][1])["tree"]
            head_tree = gitobj.parse_commit(repo.objects[head_tip][1])["tree"]
            tip_tree = gitobj.parse_commit(repo.objects[base_branch_tip][1])["tree"]
            base_files = gitobj.flatten_tree(repo.objects, base_tree)
            head_files = gitobj.flatten_tree(repo.objects, head_tree)
            tip_files = gitobj.flatten_tree(repo.objects, tip_tree)
            new_files = dict(tip_files)
            for path in set(base_files) | set(head_files):
                b = base_files.get(path)
                h = head_files.get(path)
                if b is None and h is not None:
                    new_files[path] = h
                elif b is not None and h is None:
                    new_files.pop(path, None)
                elif b is not None and h is not None and b[1] != h[1]:
                    new_files[path] = h
            root = gitobj.build_tree_from_files(new_files, repo.put_object)
            message = "%s (#%d)\n" % (pr["title"], pr["number"])
            commit = gitobj.build_commit(root, [base_branch_tip], message,
                                         COMMIT_AUTHOR, COMMIT_TS)
            commit_oid = repo.put_object("commit", commit)
            repo.refs["refs/heads/" + pr["base_ref"]] = commit_oid
            pr["state"] = "closed"
            pr["merged"] = True
            pr["merged_at"] = FIXED_TS
            pr["merge_commit_sha"] = commit_oid
            pr["closed_at"] = FIXED_TS
            return commit_oid, None


# ---------------------------------------------------------------- views

def repo_view(repo):
    return {
        "id": repo.id,
        "node_id": repo_global_id(repo.id),
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "owner": {"login": repo.owner, "id": VIEWER_ID, "type": "User"},
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "html_url": "https://example.invalid/%s/%s" % (repo.owner, repo.name),
        "url": "/repos/%s/%s" % (repo.owner, repo.name),
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "pushed_at": FIXED_TS,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "fork": False,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "license": None,
    }


def issue_view(repo, issue):
    return {
        "id": issue["id"],
        "node_id": issue_global_id(repo.id, issue["number"]),
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": n, "color": "ededed", "description": ""}
                   for n in issue["labels"]],
        "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID, "type": "User"},
        "assignee": None,
        "comments": len(issue["comments"]),
        "created_at": issue["created_at"],
        "updated_at": issue["updated_at"],
        "closed_at": issue["closed_at"],
        "html_url": "https://example.invalid/%s/%s/issues/%d"
                    % (repo.owner, repo.name, issue["number"]),
        "url": "/repos/%s/%s/issues/%d"
               % (repo.owner, repo.name, issue["number"]),
    }


def pr_view(repo, pr):
    head_tip = repo.branch_tip(pr["head_ref"])
    base_tip = repo.branch_tip(pr["base_ref"])
    return {
        "id": pr["id"],
        "node_id": pr_global_id(repo.id, pr["number"]),
        "number": pr["number"],
        "title": pr["title"],
        "body": pr["body"],
        "state": pr["state"],
        "merged": pr["merged"],
        "merged_at": pr["merged_at"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "head": {"ref": pr["head_ref"], "sha": head_tip,
                 "label": "%s:%s" % (repo.owner, pr["head_ref"]),
                 "user": {"login": repo.owner, "id": VIEWER_ID, "type": "User"}},
        "base": {"ref": pr["base_ref"], "sha": base_tip,
                 "label": "%s:%s" % (repo.owner, pr["base_ref"]),
                 "user": {"login": repo.owner, "id": VIEWER_ID, "type": "User"}},
        "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID, "type": "User"},
        "draft": False,
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
        "closed_at": pr["closed_at"],
        "html_url": "https://example.invalid/%s/%s/pull/%d"
                    % (repo.owner, repo.name, pr["number"]),
        "url": "/repos/%s/%s/pulls/%d"
              % (repo.owner, repo.name, pr["number"]),
    }


# The process-wide store: every protocol (git, REST, GraphQL, MCP) reads
# and writes this one object, so the four surfaces stay in sync.
STORE = Store()

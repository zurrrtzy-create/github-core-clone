"""REST v3 surface (/api/v3). Every reply is application/json with a
non-empty X-GitHub-Request-Id; errors carry {"message": string}.
"""
import base64
import json
import re
import uuid

from . import gitobj
from .scenario import VIEWER_LOGIN, VIEWER_ID, VIEWER_NAME, VIEWER_EMAIL
from .store import (FIXED_TS, repo_view, issue_view, pr_view)

HEX40 = re.compile(r"^[0-9a-f]{40}$")


class RestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _msg(status, message):
    return RestError(status, message)


def _parse_body(raw):
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        raise _msg(400, "Problems parsing JSON")
    if not isinstance(value, dict):
        raise _msg(400, "Problems parsing JSON")
    return value


def _sha40(value):
    return isinstance(value, str) and bool(HEX40.match(value))


def _paginate(items, query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    per_page = max(1, min(100, per_page))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    return items[start:start + per_page], per_page, page


def _link_header(base, path, query, per_page, page, total):
    pages = max(1, -(-total // per_page))
    links = []
    if page < pages:
        q = dict(query)
        q["per_page"] = [str(per_page)]
        q["page"] = [str(page + 1)]
        links.append('<%s%s?%s>; rel="next"' % (base, path, _qs(q)))
    q = dict(query)
    q["per_page"] = [str(per_page)]
    q["page"] = [str(pages)]
    links.append('<%s%s?%s>; rel="last"' % (base, path, _qs(q)))
    return ", ".join(links)


def _qs(query):
    parts = []
    for key, values in query.items():
        for v in values:
            parts.append("%s=%s" % (key, v))
    return "&".join(parts)


def _require_repo(store, owner, name):
    repo = store.get_repo(owner, name)
    if repo is None:
        raise _msg(404, "Not Found")
    return repo


def _user_view():
    return {"login": VIEWER_LOGIN, "id": VIEWER_ID, "name": VIEWER_NAME,
            "email": VIEWER_EMAIL, "type": "User",
            "html_url": "https://example.invalid/%s" % VIEWER_LOGIN}


def handle_rest(store, method, segments, query, headers, raw_body, base_url):
    """Returns (status, json_value, extra_headers). Raises RestError."""
    # -- API root -------------------------------------------------------
    if not segments:
        if method != "GET":
            raise _msg(404, "Not Found")
        return 200, {}, {"X-OAuth-Scopes": "repo, read:org"}
    if segments == ["meta"]:
        if method != "GET":
            raise _msg(404, "Not Found")
        return 200, {"installed_version": "1.0.0",
                     "verifiable_password_authentication": True}, {}
    if segments == ["user"]:
        if method != "GET":
            raise _msg(404, "Not Found")
        return 200, _user_view(), {}
    if segments == ["user", "repos"]:
        if method == "GET":
            repos = store.list_repos()
            page_items, per_page, page = _paginate([repo_view(r) for r in repos], query)
            link = _link_header(base_url, "/api/v3/user/repos", query,
                                per_page, page, len(repos))
            return 200, page_items, {"Link": link}
        if method == "POST":
            body = _parse_body(raw_body)
            name = body.get("name")
            if not isinstance(name, str) or not name:
                raise _msg(422, "Validation Failed: name is required")
            repo = store.create_repo(name, body.get("private", False),
                                     body.get("description", ""))
            if repo is None:
                raise _msg(422, "name already exists on this account")
            return 201, repo_view(repo), {}
        raise _msg(404, "Not Found")

    if len(segments) < 3 or segments[0] != "repos":
        raise _msg(404, "Not Found")
    owner, name = segments[1], segments[2]
    rest = segments[3:]

    # -- /repos/{owner}/{repo} ------------------------------------------
    if not rest:
        if method == "GET":
            return 200, repo_view(_require_repo(store, owner, name)), {}
        if method == "PATCH":
            repo = _require_repo(store, owner, name)
            body = _parse_body(raw_body)
            with store.lock:
                if "description" in body:
                    repo.description = body["description"] or ""
                if "default_branch" in body and isinstance(body["default_branch"], str):
                    repo.default_branch = body["default_branch"]
                if "private" in body:
                    repo.private = bool(body["private"])
            return 200, repo_view(repo), {}
        if method == "DELETE":
            if not store.delete_repo(owner, name):
                raise _msg(404, "Not Found")
            return 204, None, {}
        raise _msg(404, "Not Found")

    repo = _require_repo(store, owner, name)
    head = rest[0]

    # -- branches ---------------------------------------------------------
    if head == "branches":
        if method != "GET":
            raise _msg(404, "Not Found")
        if len(rest) == 1:
            out = [{"name": b, "commit": {"sha": oid},
                    "protected": False} for b, oid in repo.branches()]
            return 200, out, {}
        branch = "/".join(rest[1:])
        tip = repo.branch_tip(branch)
        if tip is None:
            raise _msg(404, "Branch not found")
        return 200, {"name": branch, "commit": {"sha": tip},
                     "protected": False}, {}

    # -- git data ---------------------------------------------------------
    if head == "git" and len(rest) >= 2 and rest[1] == "refs":
        if method == "POST" and len(rest) == 2:
            body = _parse_body(raw_body)
            ref = body.get("ref")
            sha = body.get("sha")
            if not isinstance(ref, str) or not ref.startswith("refs/"):
                raise _msg(422, "Validation Failed: ref is required")
            if not _sha40(sha) or sha not in repo.objects \
                    or repo.objects[sha][0] != "commit":
                raise _msg(422, "Validation Failed: sha is not a commit")
            with store.lock:
                repo.refs[ref] = sha
            return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, {}
        if method == "GET" and len(rest) >= 3:
            ref = "refs/" + "/".join(rest[2:])
            tip = repo.ref_tip(ref)
            if tip is None:
                raise _msg(404, "Not Found")
            otype = repo.objects.get(tip, ("commit", b""))[0]
            return 200, {"ref": ref,
                         "object": {"sha": tip, "type": otype}}, {}
        raise _msg(404, "Not Found")

    # -- commits ------------------------------------------------------------
    if head == "commits" and method == "GET":
        branch = query.get("sha", [repo.default_branch])[0]
        if repo.branch_tip(branch) is None:
            raise _msg(404, "Not Found")
        items = store.list_commits(repo, branch)
        out = []
        for c in items:
            out.append({"sha": c["sha"],
                        "commit": {"message": c["message"],
                                   "author": {"name": c["author"]},
                                   "committer": {"name": c["committer"]}}})
        return 200, out, {}

    # -- contents -----------------------------------------------------------
    if head == "contents":
        path = "/".join(rest[1:])
        branch = query.get("ref", [repo.default_branch])[0]
        if method == "GET":
            found = store.read_path(repo, branch, path)
            if found is None:
                raise _msg(404, "Not Found")
            if found[0] == "dir":
                out = []
                for mode, ename, oid in found[1]:
                    obj = repo.objects.get(oid)
                    entry = {"type": "dir" if mode == "40000" else "file",
                             "name": ename,
                             "path": (path + "/" + ename) if path else ename,
                             "sha": oid}
                    if mode != "40000" and obj is not None:
                        entry["size"] = len(obj[1])
                    out.append(entry)
                return 200, out, {}
            oid, data = found[1]
            return 200, {"type": "file", "encoding": "base64",
                         "size": len(data), "name": path.split("/")[-1],
                         "path": path,
                         "content": base64.b64encode(data).decode("ascii"),
                         "sha": oid}, {}
        if method == "PUT":
            body = _parse_body(raw_body)
            message = body.get("message", "")
            content = body.get("content")
            if not isinstance(content, str):
                raise _msg(422, "Validation Failed: content is required")
            try:
                data = base64.b64decode(content, validate=False)
            except Exception:
                raise _msg(422, "Validation Failed: content is not base64")
            put_branch = body.get("branch") or branch
            sha = body.get("sha")
            found = store.read_path(repo, put_branch, path)
            if found is not None and found[0] == "file":
                if not _sha40(sha) or sha != found[1][0]:
                    raise _msg(409, "Conflict: sha does not match the file")
                status = 200
            elif found is not None:
                raise _msg(422, "Validation Failed: path is a directory")
            else:
                status = 201
            commit_oid, _created = store.commit_files(
                repo, put_branch, {path: data},
                message if isinstance(message, str) else "")
            blob_oid = gitobj.hash_object("blob", data)
            return status, {"content": {"name": path.split("/")[-1],
                                        "path": path, "sha": blob_oid,
                                        "size": len(data),
                                        "encoding": "base64"},
                            "commit": {"sha": commit_oid,
                                       "message": message}}, {}
        if method == "DELETE":
            body = _parse_body(raw_body)
            message = body.get("message", "")
            sha = body.get("sha")
            found = store.read_path(repo, branch, path)
            if found is None or found[0] != "file":
                raise _msg(404, "Not Found")
            if not _sha40(sha) or sha != found[1][0]:
                raise _msg(409, "Conflict: sha does not match the file")
            commit_oid, _c = store.commit_files(
                repo, branch, {path: None},
                message if isinstance(message, str) else "")
            return 200, {"commit": {"sha": commit_oid,
                                    "message": message}}, {}
        raise _msg(404, "Not Found")

    # -- labels -------------------------------------------------------------
    if head == "labels":
        if method == "GET":
            return 200, list(repo.labels.values()), {}
        if method == "POST":
            body = _parse_body(raw_body)
            lname = body.get("name")
            if not isinstance(lname, str) or not lname:
                raise _msg(422, "Validation Failed: name is required")
            label = store.create_label(repo, lname,
                                       body.get("color", "ededed"),
                                       body.get("description", ""))
            return 201, label, {}
        raise _msg(404, "Not Found")

    # -- issues ---------------------------------------------------------------
    if head == "issues":
        if len(rest) == 1:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                direction = query.get("direction", ["desc"])[0]
                issues = store.list_issues(repo, state, direction)
                items, _pp, _pg = _paginate([issue_view(repo, i) for i in issues], query)
                return 200, items, {}
            if method == "POST":
                body = _parse_body(raw_body)
                title = body.get("title")
                if not isinstance(title, str) or not title:
                    raise _msg(422, "Validation Failed: title is required")
                labels = body.get("labels") or []
                if not isinstance(labels, list):
                    labels = []
                label_names = []
                for entry in labels:
                    lname = entry.get("name") if isinstance(entry, dict) else entry
                    if not isinstance(lname, str) or lname not in repo.labels:
                        raise _msg(422, "Validation Failed: label does not exist")
                    label_names.append(lname)
                issue = store.create_issue(repo, title, body.get("body", ""),
                                           label_names)
                return 201, issue_view(repo, issue), {}
            raise _msg(404, "Not Found")
        try:
            number = int(rest[1])
        except ValueError:
            raise _msg(404, "Not Found")
        issue = repo.issues.get(number)
        if issue is None:
            raise _msg(404, "Not Found")
        if len(rest) == 2:
            if method == "GET":
                return 200, issue_view(repo, issue), {}
            if method == "PATCH":
                body = _parse_body(raw_body)
                with store.lock:
                    if "state" in body and body["state"] in ("open", "closed"):
                        issue["state"] = body["state"]
                        issue["closed_at"] = FIXED_TS if body["state"] == "closed" else None
                    if "title" in body and isinstance(body["title"], str):
                        issue["title"] = body["title"]
                    if "body" in body and isinstance(body["body"], str):
                        issue["body"] = body["body"]
                    if "labels" in body and isinstance(body["labels"], list):
                        names = []
                        for entry in body["labels"]:
                            lname = entry.get("name") if isinstance(entry, dict) else entry
                            if isinstance(lname, str) and lname in repo.labels:
                                names.append(lname)
                        issue["labels"] = names
                    issue["updated_at"] = FIXED_TS
                return 200, issue_view(repo, issue), {}
            raise _msg(404, "Not Found")
        if len(rest) == 3 and rest[2] == "comments":
            if method == "GET":
                out = [{"id": c["id"], "body": c["body"],
                        "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}}
                       for c in issue["comments"]]
                return 200, out, {}
            if method == "POST":
                body = _parse_body(raw_body)
                cbody = body.get("body")
                if not isinstance(cbody, str) or not cbody:
                    raise _msg(422, "Validation Failed: body is required")
                with store.lock:
                    cid = store.next_object_id
                    store.next_object_id += 1
                    comment = {"id": cid, "body": cbody}
                    issue["comments"].append(comment)
                return 201, {"id": cid, "body": cbody,
                             "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}}, {}
            raise _msg(404, "Not Found")
        raise _msg(404, "Not Found")

    # -- pull requests ---------------------------------------------------------
    if head == "pulls":
        if len(rest) == 1:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                pulls = [p for p in repo.pulls.values()
                         if state == "all" or p["state"] == state]
                pulls.sort(key=lambda p: p["number"], reverse=True)
                items, _pp, _pg = _paginate([pr_view(repo, p) for p in pulls], query)
                return 200, items, {}
            if method == "POST":
                body = _parse_body(raw_body)
                title = body.get("title")
                head_ref = body.get("head")
                base_ref = body.get("base") or repo.default_branch
                if not isinstance(title, str) or not title:
                    raise _msg(422, "Validation Failed: title is required")
                if not isinstance(head_ref, str) or not head_ref:
                    raise _msg(422, "Validation Failed: head is required")
                pr, err = store.create_pr(repo, title, body.get("body", ""),
                                          head_ref, base_ref)
                if pr is None:
                    if err == "exists":
                        raise _msg(422, "Validation Failed: pull request already exists")
                    raise _msg(422, "Validation Failed: head or base branch not found")
                return 201, pr_view(repo, pr), {}
            raise _msg(404, "Not Found")
        try:
            number = int(rest[1])
        except ValueError:
            raise _msg(404, "Not Found")
        pr = repo.pulls.get(number)
        if pr is None:
            raise _msg(404, "Not Found")
        if len(rest) == 2:
            if method == "GET":
                return 200, pr_view(repo, pr), {}
            raise _msg(404, "Not Found")
        if len(rest) == 3 and rest[2] == "files" and method == "GET":
            out = [{"filename": path, "status": status,
                    "additions": add, "deletions": dele, "changes": ch}
                   for path, status, add, dele, ch in store.pr_diff(repo, pr)]
            return 200, out, {}
        if len(rest) == 3 and rest[2] == "merge" and method == "PUT":
            _parse_body(raw_body)
            commit_oid, err = store.merge_pr(repo, pr)
            if commit_oid is None:
                if err == "not open":
                    raise _msg(405, "Method Not Allowed: pull request is not open")
                raise _msg(405, "Method Not Allowed: merge conflict")
            return 200, {"merged": True, "sha": commit_oid,
                         "message": "Pull Request successfully merged"}, {}
        raise _msg(404, "Not Found")

    raise _msg(404, "Not Found")

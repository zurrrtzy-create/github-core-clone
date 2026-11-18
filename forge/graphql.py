"""GraphQL v4 executor for the fixed document set gh and github-mcp-server
send. A small recursive-descent parser for the query language subset the
suite uses (operations, variables, aliases, inline fragments, inline input
objects/lists/enums, batched arrays) and a resolver over the store.
"""
import base64
import json
import re

from .scenario import VIEWER_LOGIN, VIEWER_ID
from .store import (repo_global_id, issue_global_id, pr_global_id,
                    comment_global_id, parse_global_id)

# ---------------------------------------------------------------- tokenizer

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<spread>\.\.\.)
  | (?P<punct>[{}()\[\]:!=$@,])
  | (?P<float>-?\d+\.\d+(?:[eE][+-]?\d+)?)
  | (?P<int>-?\d+)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
""", re.X)


def _tokenize(text):
    tokens = []
    i = 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise ValueError("cannot tokenize GraphQL at %d" % i)
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, m.group()))
    tokens.append(("eof", ""))
    return tokens


def _unquote(raw):
    return json.loads(raw)


# ---------------------------------------------------------------- parser

class _Parser:
    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_punct(self, ch):
        kind, val = self.next()
        if kind != "punct" or val != ch:
            raise ValueError("expected %s, got %s" % (ch, val))

    def parse_document(self):
        ops = []
        while self.peek()[0] != "eof":
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        kind, val = self.next()
        if kind == "punct" and val == "{":
            return {"kind": "query", "name": None, "vars": [],
                    "selections": self.parse_selections()}
        if kind != "name" or val not in ("query", "mutation", "subscription"):
            raise ValueError("expected operation, got %s" % val)
        op_kind = val
        name = None
        if self.peek()[0] == "name":
            name = self.next()[1]
        vardefs = []
        if self.peek() == ("punct", "("):
            self.next()
            while self.peek() != ("punct", ")"):
                if self.peek() == ("punct", ","):
                    self.next()
                    continue
                vardefs.append(self.parse_vardef())
            self.next()
        self.expect_punct("{")
        return {"kind": op_kind, "name": name, "vars": vardefs,
                "selections": self.parse_selections()}

    def parse_vardef(self):
        kind, val = self.next()  # $
        if kind != "punct" or val != "$":
            raise ValueError("expected $, got %s" % val)
        name = self.next()[1]
        self.expect_punct(":")
        self.parse_type()
        if self.peek() == ("punct", "="):
            self.next()
            self.parse_value()
        return name

    def parse_type(self):
        if self.peek() == ("punct", "["):
            self.next()
            self.parse_type()
            self.expect_punct("]")
        else:
            self.next()  # type name
        if self.peek() == ("punct", "!"):
            self.next()

    def parse_selections(self):
        sels = []
        while self.peek() != ("punct", "}"):
            sels.append(self.parse_selection())
        self.next()  # }
        return sels

    def parse_selection(self):
        if self.peek()[0] == "spread":
            self.next()
            if self.peek() == ("name", "on"):
                self.next()
                type_name = self.next()[1]
                self.expect_punct("{")
                return {"fragment": type_name,
                        "selections": self.parse_selections()}
            raise ValueError("named fragments are not supported")
        name = self.next()[1]
        alias = None
        if self.peek() == ("punct", ":"):
            self.next()
            alias = name
            name = self.next()[1]
        args = {}
        if self.peek() == ("punct", "("):
            self.next()
            while self.peek() != ("punct", ")"):
                if self.peek() == ("punct", ","):
                    self.next()
                    continue
                arg_name = self.next()[1]
                self.expect_punct(":")
                args[arg_name] = self.parse_value()
            self.next()
        # directives: skip @name[(args)]
        while self.peek() == ("punct", "@"):
            self.next()
            self.next()
            if self.peek() == ("punct", "("):
                depth = 0
                while True:
                    kind, val = self.next()
                    if kind == "punct" and val == "(":
                        depth += 1
                    elif kind == "punct" and val == ")":
                        depth -= 1
                        if depth == 0:
                            break
        selections = None
        if self.peek() == ("punct", "{"):
            self.next()
            selections = self.parse_selections()
        return {"field": name, "alias": alias or name, "args": args,
                "selections": selections}

    def parse_value(self):
        kind, val = self.next()
        if kind == "punct" and val == "$":
            return ("var", self.next()[1])
        if kind == "punct" and val == "[":
            items = []
            while self.peek() != ("punct", "]"):
                if self.peek() == ("punct", ","):
                    self.next()
                    continue
                items.append(self.parse_value())
            self.next()
            return ("list", items)
        if kind == "punct" and val == "{":
            obj = {}
            while self.peek() != ("punct", "}"):
                if self.peek() == ("punct", ","):
                    self.next()
                    continue
                key = self.next()[1]
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.next()
            return ("object", obj)
        if kind == "string":
            return ("literal", _unquote(val))
        if kind == "int":
            return ("literal", int(val))
        if kind == "float":
            return ("literal", float(val))
        if kind == "name":
            if val == "true":
                return ("literal", True)
            if val == "false":
                return ("literal", False)
            if val == "null":
                return ("literal", None)
            return ("enum", val)
        raise ValueError("bad value %s %s" % (kind, val))


def _resolve_value(value, variables):
    kind, val = value
    if kind == "var":
        return variables.get(val)
    if kind == "list":
        return [_resolve_value(v, variables) for v in val]
    if kind == "object":
        return {k: _resolve_value(v, variables) for k, v in val.items()}
    return val


# ---------------------------------------------------------------- schema (introspection)

_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "createdAt",
              "updatedAt", "closedAt", "author", "labels", "comments", "url"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged",
                    "mergeable", "headRefName", "baseRefName", "headRefOid",
                    "baseRefOid", "files", "mergedAt", "mergeCommit",
                    "author", "url"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission",
                   "hasIssuesEnabled", "issue", "issues", "pullRequests",
                   "pullRequest", "description", "isPrivate", "url",
                   "createdAt"],
    "User": ["id", "login", "name", "email"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "endCursor", "startCursor"],
    "PullRequestChangedFile": ["path", "additions", "deletions", "changes"],
    "IssueComment": ["id", "body", "author"],
}
_TYPE_ENUMS = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "COMMIT"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "OrderDirection": ["ASC", "DESC"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
}


def _cursor(number):
    return base64.urlsafe_b64encode(("cursor:%d" % number).encode()).decode()


def _uncursor(cursor):
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
        if text.startswith("cursor:"):
            return int(text[len("cursor:"):])
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- executor

class GraphQLError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _Executor:
    def __init__(self, store):
        self.store = store
        self.errors = []

    def execute(self, operation, variables):
        self.errors = []
        if operation["kind"] == "mutation":
            data = self._exec_selections(
                operation["selections"], ("Mutation", None), variables)
        else:
            data = self._exec_selections(
                operation["selections"], ("Query", None), variables)
        result = {"data": data}
        if self.errors:
            result["errors"] = [{"message": m} for m in self.errors]
        return result

    # -- selection --------------------------------------------------------
    def _exec_selections(self, selections, obj, variables):
        out = {}
        for sel in selections:
            if "fragment" in sel:
                if self._type_matches(sel["fragment"], obj):
                    out.update(self._exec_selections(sel["selections"], obj,
                                                     variables))
                continue
            out[sel["alias"]] = self._exec_field(sel, obj, variables)
        return out

    def _type_matches(self, type_name, obj):
        kind, _value = obj
        if type_name == "Repository":
            return kind == "Repository"
        if type_name == "Issue":
            return kind == "Issue"
        if type_name == "PullRequest":
            return kind == "PullRequest"
        if type_name == "User":
            return kind == "User"
        return kind == type_name

    def _exec_field(self, sel, obj, variables):
        kind, value = obj
        field = sel["field"]
        args = {k: _resolve_value(v, variables) for k, v in sel["args"].items()}
        try:
            if kind == "Query":
                return self._query_field(field, args, sel, variables)
            if kind == "Mutation":
                return self._mutation_field(field, args, sel, variables)
            if kind == "Repository":
                return self._repo_field(value, field, args, sel, variables)
            if kind == "Issue":
                return self._issue_field(value, field, args, sel, variables)
            if kind == "PullRequest":
                return self._pr_field(value, field, args, sel, variables)
            if kind == "User":
                return self._user_field(value, field, args, sel, variables)
            if kind == "IssueConnection" or kind == "PullRequestConnection":
                return self._connection_field(value, field, args, sel, variables)
            if kind == "PageInfo":
                return value.get(field)
            if kind == "__Type":
                return self._type_field(value, field, args, sel, variables)
            if kind == "Ref":
                return value.get(field)
            if kind == "ChangedFile":
                return value.get(field)
            if kind == "CommentEdge":
                return self._exec_selections(sel["selections"] or [],
                                             ("Comment", value), variables)
            if kind == "Comment":
                return value.get(field)
            if kind == "CreateIssuePayload" or kind == "CloseIssuePayload":
                if field == "issue":
                    issue = value
                    return self._exec_selections(
                        sel["selections"] or [], ("Issue", issue), variables)
                return None
            if kind == "CreatePRPayload" or kind == "MergePRPayload":
                if field == "pullRequest":
                    pr = value
                    return self._exec_selections(
                        sel["selections"] or [], ("PullRequest", pr), variables)
                return None
            if kind == "AddCommentPayload":
                if field == "commentEdge":
                    return self._exec_selections(
                        sel["selections"] or [], ("CommentEdge", value), variables)
                return None
        except GraphQLError as err:
            self.errors.append(err.message)
            return None
        self.errors.append(
            'Cannot query field "%s" on type "%s".' % (field, kind))
        return None

    # -- root ---------------------------------------------------------------
    def _query_field(self, field, args, sel, variables):
        if field == "viewer":
            user = {"login": VIEWER_LOGIN, "id": VIEWER_ID}
            return self._exec_selections(sel["selections"] or [],
                                         ("User", user), variables)
        if field == "repository":
            repo = self.store.get_repo(args.get("owner"), args.get("name"))
            if repo is None:
                return None
            return self._exec_selections(sel["selections"] or [],
                                         ("Repository", repo), variables)
        if field == "node":
            gid = args.get("id")
            gkind, nums = parse_global_id(gid)
            if gkind == "Repository" and nums:
                repo = self.store.get_repo_by_id(nums[0])
                if repo is None:
                    return None
                return self._exec_selections(sel["selections"] or [],
                                             ("Repository", repo), variables)
            if gkind == "Issue" and len(nums) == 2:
                repo = self.store.get_repo_by_id(nums[0])
                issue = repo.issues.get(nums[1]) if repo else None
                if issue is None:
                    return None
                return self._exec_selections(
                    sel["selections"] or [],
                    ("Issue", (repo, issue)), variables)
            if gkind == "PullRequest" and len(nums) == 2:
                repo = self.store.get_repo_by_id(nums[0])
                pr = repo.pulls.get(nums[1]) if repo else None
                if pr is None:
                    return None
                return self._exec_selections(
                    sel["selections"] or [],
                    ("PullRequest", (repo, pr)), variables)
            return None
        if field == "__type":
            name = args.get("name")
            if name in _TYPE_FIELDS or name in _TYPE_ENUMS:
                return self._exec_selections(
                    sel["selections"] or [], ("__Type", name), variables)
            return None
        if field == "__schema":
            return None
        raise GraphQLError(
            'Cannot query field "%s" on type "Query".' % field)

    # -- mutations ------------------------------------------------------------
    def _mutation_field(self, field, args, sel, variables):
        store = self.store
        if field == "createIssue":
            inp = args.get("input") or {}
            gkind, nums = parse_global_id(inp.get("repositoryId"))
            repo = store.get_repo_by_id(nums[0]) if gkind == "Repository" and nums else None
            if repo is None:
                raise GraphQLError("repository not found")
            issue = store.create_issue(repo, inp.get("title", ""),
                                       inp.get("body", ""))
            return self._exec_selections(
                sel["selections"] or [],
                ("CreateIssuePayload", (repo, issue)), variables)
        if field == "closeIssue":
            inp = args.get("input") or {}
            gkind, nums = parse_global_id(inp.get("issueId"))
            repo = store.get_repo_by_id(nums[0]) if gkind == "Issue" and len(nums) == 2 else None
            issue = repo.issues.get(nums[1]) if repo else None
            if issue is None:
                raise GraphQLError("issue not found")
            with store.lock:
                issue["state"] = "closed"
            return self._exec_selections(
                sel["selections"] or [],
                ("CloseIssuePayload", (repo, issue)), variables)
        if field == "createPullRequest":
            inp = args.get("input") or {}
            gkind, nums = parse_global_id(inp.get("repositoryId"))
            repo = store.get_repo_by_id(nums[0]) if gkind == "Repository" and nums else None
            if repo is None:
                raise GraphQLError("repository not found")
            pr, err = store.create_pr(repo, inp.get("title", ""),
                                      inp.get("body", ""),
                                      inp.get("headRefName", ""),
                                      inp.get("baseRefName", "main"))
            if pr is None:
                raise GraphQLError("cannot create pull request: %s" % err)
            return self._exec_selections(
                sel["selections"] or [],
                ("CreatePRPayload", (repo, pr)), variables)
        if field == "mergePullRequest":
            inp = args.get("input") or {}
            gkind, nums = parse_global_id(inp.get("pullRequestId"))
            repo = store.get_repo_by_id(nums[0]) if gkind == "PullRequest" and len(nums) == 2 else None
            pr = repo.pulls.get(nums[1]) if repo else None
            if pr is None:
                raise GraphQLError("pull request not found")
            commit_oid, err = store.merge_pr(repo, pr)
            if commit_oid is None:
                raise GraphQLError("cannot merge pull request: %s" % err)
            return self._exec_selections(
                sel["selections"] or [],
                ("MergePRPayload", (repo, pr)), variables)
        if field == "addComment":
            inp = args.get("input") or {}
            gkind, nums = parse_global_id(inp.get("subjectId"))
            repo = store.get_repo_by_id(nums[0]) if gkind == "Issue" and len(nums) == 2 else None
            issue = repo.issues.get(nums[1]) if repo else None
            if issue is None:
                raise GraphQLError("subject not found")
            with store.lock:
                cid = store.next_object_id
                store.next_object_id += 1
                comment = {"id": cid, "body": inp.get("body", "")}
                issue["comments"].append(comment)
            edge = {"id": comment_global_id(repo.id, issue["number"], cid),
                    "body": inp.get("body", "")}
            return self._exec_selections(
                sel["selections"] or [],
                ("AddCommentPayload", edge), variables)
        raise GraphQLError(
            'Cannot query field "%s" on type "Mutation".' % field)

    # -- objects ---------------------------------------------------------------
    def _repo_field(self, repo, field, args, sel, variables):
        if field == "id":
            return repo_global_id(repo.id)
        if field == "databaseId":
            return repo.id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name)
        if field == "owner":
            user = {"login": repo.owner, "id": VIEWER_ID}
            return self._exec_selections(sel["selections"] or [],
                                         ("User", user), variables)
        if field == "defaultBranchRef":
            return self._exec_selections(
                sel["selections"] or [],
                ("Ref", {"name": repo.default_branch}), variables)
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "description":
            return repo.description
        if field == "isPrivate":
            return repo.private
        if field == "url":
            return "https://example.invalid/%s/%s" % (repo.owner, repo.name)
        if field == "createdAt":
            return "2025-01-01T00:00:00Z"
        if field == "issue":
            issue = repo.issues.get(args.get("number"))
            if issue is None:
                return None
            return self._exec_selections(sel["selections"] or [],
                                         ("Issue", (repo, issue)), variables)
        if field == "issues":
            return self._issues_connection(repo, args, sel, variables)
        if field == "pullRequests":
            pulls = sorted(repo.pulls.values(), key=lambda p: p["number"],
                           reverse=True)
            first = args.get("first")
            if isinstance(first, int):
                pulls = pulls[:first]
            conn = {"nodes": [("PullRequest", (repo, p)) for p in pulls]}
            return self._exec_selections(sel["selections"] or [],
                                         ("PullRequestConnection", conn),
                                         variables)
        if field == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            if pr is None:
                return None
            return self._exec_selections(sel["selections"] or [],
                                         ("PullRequest", (repo, pr)), variables)
        raise GraphQLError(
            'Cannot query field "%s" on type "Repository".' % field)

    def _issues_connection(self, repo, args, sel, variables):
        issues = list(repo.issues.values())
        states = args.get("states")
        if states:
            wanted = {"OPEN" if s == "OPEN" else "CLOSED" for s in states}
            # states arrive as enum strings; map to stored lowercase
            mapped = set()
            for s in states:
                mapped.add("open" if s == "OPEN" else "closed")
            issues = [i for i in issues if i["state"] in mapped]
        order_by = args.get("orderBy") or {}
        direction = order_by.get("direction", "ASC")
        issues.sort(key=lambda i: i["number"],
                    reverse=(direction == "DESC"))
        after = args.get("after")
        if after:
            num = _uncursor(after)
            if num is not None:
                issues = [i for i in issues
                          if (i["number"] > num if direction != "DESC"
                              else i["number"] < num)]
        first = args.get("first")
        page = issues if not isinstance(first, int) else issues[:first]
        has_next = len(issues) > len(page)
        end_cursor = _cursor(page[-1]["number"]) if page else None
        conn = {"nodes": [("Issue", (repo, i)) for i in page],
                "pageInfo": {"hasNextPage": has_next,
                             "hasPreviousPage": False,
                             "endCursor": end_cursor,
                             "startCursor": _cursor(page[0]["number"]) if page else None}}
        return self._exec_selections(sel["selections"] or [],
                                     ("IssueConnection", conn), variables)

    def _issue_field(self, pair, field, args, sel, variables):
        repo, issue = pair
        if field == "id":
            return issue_global_id(repo.id, issue["number"])
        if field == "number":
            return issue["number"]
        if field == "title":
            return issue["title"]
        if field == "state":
            return "OPEN" if issue["state"] == "open" else "CLOSED"
        if field == "body":
            return issue["body"]
        if field == "createdAt" or field == "updatedAt":
            return issue["created_at"]
        if field == "closedAt":
            return issue["closed_at"]
        if field == "url":
            return "https://example.invalid/%s/%s/issues/%d" % (
                repo.owner, repo.name, issue["number"])
        raise GraphQLError(
            'Cannot query field "%s" on type "Issue".' % field)

    def _pr_field(self, pair, field, args, sel, variables):
        repo, pr = pair
        if field == "id":
            return pr_global_id(repo.id, pr["number"])
        if field == "number":
            return pr["number"]
        if field == "title":
            return pr["title"]
        if field == "body":
            return pr["body"]
        if field == "state":
            if pr["merged"]:
                return "MERGED"
            return "OPEN" if pr["state"] == "open" else "CLOSED"
        if field == "merged":
            return bool(pr["merged"])
        if field == "mergeable":
            return self.store.pr_mergeable(repo, pr)
        if field == "headRefName":
            return pr["head_ref"]
        if field == "baseRefName":
            return pr["base_ref"]
        if field == "headRefOid":
            return repo.branch_tip(pr["head_ref"])
        if field == "baseRefOid":
            return repo.branch_tip(pr["base_ref"])
        if field == "mergedAt":
            return pr["merged_at"]
        if field == "files":
            first = args.get("first")
            files = self.store.pr_diff(repo, pr)
            nodes = [{"path": path} for path, _s, _a, _d, _c in files]
            if isinstance(first, int):
                nodes = nodes[:first]
            conn = {"nodes": [("ChangedFile", n) for n in nodes]}
            return self._exec_selections(sel["selections"] or [],
                                         ("PullRequestConnection", conn),
                                         variables)
        if field == "url":
            return "https://example.invalid/%s/%s/pull/%d" % (
                repo.owner, repo.name, pr["number"])
        raise GraphQLError(
            'Cannot query field "%s" on type "PullRequest".' % field)

    def _user_field(self, user, field, args, sel, variables):
        if field == "login":
            return user["login"]
        if field == "id":
            return base64.urlsafe_b64encode(
                ("User:%d" % user["id"]).encode()).decode()
        if field == "name":
            return user["login"]
        if field == "email":
            return "%s@example.invalid" % user["login"]
        raise GraphQLError(
            'Cannot query field "%s" on type "User".' % field)

    def _connection_field(self, conn, field, args, sel, variables):
        if field == "nodes":
            out = []
            for node in conn.get("nodes", []):
                kind, value = node
                # nodes are (kind, value) pairs already
                out.append(self._exec_selections(sel["selections"] or [],
                                                 node, variables))
            return out
        if field == "pageInfo":
            return self._exec_selections(sel["selections"] or [],
                                         ("PageInfo", conn.get("pageInfo", {})),
                                         variables)
        if field == "totalCount":
            return len(conn.get("nodes", []))
        raise GraphQLError(
            'Cannot query field "%s" on type "Connection".' % field)

    def _type_field(self, name, field, args, sel, variables):
        if field == "name":
            return name
        if field == "kind":
            return "ENUM" if name in _TYPE_ENUMS else "OBJECT"
        if field == "fields":
            names = _TYPE_FIELDS.get(name, [])
            return [{"name": n} for n in names]
        if field == "enumValues":
            names = _TYPE_ENUMS.get(name, [])
            return [{"name": n} for n in names]
        if field == "description":
            return None
        raise GraphQLError(
            'Cannot query field "%s" on type "__Type".' % field)


def handle_graphql(store, body):
    """Execute one operation or a batch; returns the response JSON value."""
    if isinstance(body, list):
        return [handle_graphql(store, entry) for entry in body]
    if not isinstance(body, dict):
        return {"data": None,
                "errors": [{"message": "request body must be an object"}]}
    query = body.get("query")
    variables = body.get("variables") or {}
    if not isinstance(query, str):
        return {"data": None,
                "errors": [{"message": "query must be a string"}]}
    try:
        operations = _Parser(query).parse_document()
    except ValueError as err:
        return {"data": None, "errors": [{"message": str(err)}]}
    if not operations:
        return {"data": None,
                "errors": [{"message": "empty document"}]}
    operation = operations[0]
    op_name = body.get("operationName")
    if op_name:
        for op in operations:
            if op.get("name") == op_name:
                operation = op
                break
    return _Executor(store).execute(operation, variables)

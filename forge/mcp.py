"""MCP over Streamable HTTP: JSON-RPC 2.0, server/discover + initialize +
tools/list + tools/call, acting on the same store as REST/GraphQL.
"""
import json
import re

from . import rest
from .store import STORE

SERVER_NAME = "github-forge"
SERVER_VERSION = "1.0.0"
DISCOVER_VERSIONS = ["2026-07-28", "2025-11-25"]
PROTOCOL_VERSION = "2026-07-28"


def _server_info():
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def _tools():
    return [
        {
            "name": "create_repo",
            "description": "Create a new repository under the authenticated user.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Repository name"},
                    "description": {"type": "string", "description": "Repository description"},
                    "private": {"type": "boolean", "description": "Whether the repository is private"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_repo",
            "description": "Get a repository by owner and name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner login"},
                    "repo": {"type": "string", "description": "Repository name"},
                },
                "required": ["owner", "repo"],
            },
        },
        {
            "name": "create_issue",
            "description": "Create an issue in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner login"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body"},
                },
                "required": ["owner", "repo", "title"],
            },
        },
        {
            "name": "list_issues",
            "description": "List issues in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner login"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "state": {"type": "string", "description": "open, closed or all"},
                },
                "required": ["owner", "repo"],
            },
        },
        {
            "name": "create_pull_request",
            "description": "Create a pull request in a repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner login"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "title": {"type": "string", "description": "Pull request title"},
                    "head": {"type": "string", "description": "Head branch name"},
                    "base": {"type": "string", "description": "Base branch name"},
                    "body": {"type": "string", "description": "Pull request body"},
                },
                "required": ["owner", "repo", "title", "head", "base"],
            },
        },
        {
            "name": "merge_pull_request",
            "description": "Merge a pull request using the squash method.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner login"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "pull_number": {"type": "integer", "description": "Pull request number"},
                },
                "required": ["owner", "repo", "pull_number"],
            },
        },
    ]


# Byte-identical across calls: built once, serialized deterministically.
TOOLS_JSON = json.dumps(_tools(), sort_keys=True, separators=(",", ":"))
TOOLS = json.loads(TOOLS_JSON)

REQUIRED = {t["name"]: t["inputSchema"]["required"] for t in TOOLS}


def _result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload}


def _error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(name, args):
    missing = [f for f in REQUIRED.get(name, []) if f not in args
               or args[f] is None]
    if missing:
        return _error_result("Missing required argument(s): %s"
                             % ", ".join(missing))
    if name == "create_repo":
        repo = STORE.create_repo(args["name"], args.get("private", False),
                                 args.get("description", ""))
        if repo is None:
            return _error_result(
                "Validation Failed: name already exists on this account")
        return _result(rest.repo_view(repo))
    if name == "get_repo":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return _error_result("Not Found")
        return _result(rest.repo_view(repo))
    repo = STORE.get_repo(args["owner"], args["repo"])
    if repo is None:
        return _error_result("Not Found")
    if name == "create_issue":
        issue = STORE.create_issue(repo, args["title"], args.get("body", ""),
                                   [])
        return _result(rest.issue_view(repo, issue))
    if name == "list_issues":
        state = args.get("state", "open")
        issues = STORE.list_issues(repo, state, "asc")
        return _result([rest.issue_view(repo, i) for i in issues])
    if name == "create_pull_request":
        pr, err = STORE.create_pr(repo, args["title"], args.get("body", ""),
                                  args["head"], args["base"])
        if pr is None:
            return _error_result(err or "Unprocessable Entity")
        return _result(rest.pr_view(repo, pr))
    if name == "merge_pull_request":
        pr = repo.pulls.get(args["pull_number"])
        if pr is None:
            return _error_result("Not Found")
        commit_oid, err = STORE.merge_pr(repo, pr, "squash")
        if commit_oid is None:
            return _error_result(err or "Unprocessable Entity")
        return _result({"merged": True, "sha": commit_oid,
                        "number": pr["number"],
                        "message": "Pull request successfully merged"})
    return None


def handle_mcp(body):
    """Returns (status, response_dict_or_None)."""
    try:
        text = body.decode("utf-8")
        req = json.loads(text)
    except Exception:
        req_id = None
        m = re.search(r'"id"\s*:\s*(\d+|"[^"]*")', body.decode("utf-8", "replace"))
        if m:
            raw = m.group(1)
            try:
                req_id = json.loads(raw)
            except Exception:
                req_id = None
        return 400, {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(req, dict) or "method" not in req:
        return 200, {"jsonrpc": "2.0",
                     "id": req.get("id") if isinstance(req, dict) else None,
                     "error": {"code": -32600, "message": "Invalid Request"}}
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "notifications/initialized":
        return 202, None
    if method == "server/discover":
        info = _server_info()
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {
            "supportedVersions": DISCOVER_VERSIONS,
            "capabilities": {"tools": {}},
            "serverInfo": info,
            "instructions": ("A GitHub-compatible git forge exposing "
                             "repositories, issues and pull requests."),
            "_meta": {"io.modelcontextprotocol/serverInfo": info}}}
    if method == "initialize":
        offered = None
        if isinstance(params, dict):
            offered = params.get("protocolVersion")
        version = offered if offered in DISCOVER_VERSIONS else PROTOCOL_VERSION
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": _server_info()}}
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": req_id,
                     "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return 200, {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32602,
                                   "message": "Invalid params"}}
        if name not in REQUIRED:
            return 200, {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32602,
                                   "message": "Unknown tool: %s" % name}}
        try:
            result = _call_tool(name, args)
        except rest.RestError as err:
            result = _error_result(err.message)
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}
    return 200, {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32601, "message": "Method not found"}}

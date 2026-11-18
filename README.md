# github-core-clone

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/zurrrtzy-create/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/zurrrtzy-create/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

GitHub-compatible git forge — pure Python 3.13 stdlib (smart-HTTP git, REST v3, GraphQL, MCP) — make.fun bounty github-core-clone-01.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=2ehgyxhw&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>

A GitHub-compatible git forge in pure Python 3.13 (standard library only):
smart-HTTP git (protocol v2 and v0), the REST v3 API, GraphQL v4, and MCP
over Streamable HTTP — all four surfaces reading and writing one shared
in-memory store, so a repository created over REST is clonable over git,
an issue created over MCP shows up over GraphQL, and a pull request merged
over GraphQL is visible over REST.

## Running

```sh
python3 main.py
```

In a make.fun trial the environment sets `MAKEFUN_ENV_URL`; the forge binds
an ephemeral port, registers it, waits for `/done`, and exits 0. For local
testing, set `FORGE_STANDALONE=1` (and `MAKEFUN_SHOP_TOKEN` for the bearer
token the API surfaces expect):

```sh
MAKEFUN_SHOP_TOKEN=testtoken FORGE_STANDALONE=1 python3 main.py
# github-forge listening on http://127.0.0.1:PORT
```

## Layout

```
main.py            entrypoint: bind port 0, register, poll /done
forge/
  gitobj.py        SHA-1 objects, packfiles, pkt-lines, shallow/depth sets
  scenario.py      the deterministic scenario RNG (viewer adaiversen, …)
  store.py         the shared store: repos, refs, issues, pull requests
  git_http.py      smart-HTTP: advertisements, v2/v0 fetch, receive-pack
  rest.py          REST v3 dispatcher (/api/v3)
  graphql.py       GraphQL tokenizer, parser and executor (/api/graphql)
  mcp.py           MCP JSON-RPC 2.0: server/discover, tools/list, tools/call
  server.py        ThreadingHTTPServer routing + auth + request ids
```

## Surfaces

* **Git** — `/{owner}/{repo}.git/info/refs`, `git-upload-pack`,
  `git-receive-pack`. Protocol v2 (`version 2` capability advertisement,
  `ls-refs`, `fetch` with `shallow`/`deepen`, sideband packfiles) and v0
  (advertisements, fetch, push with `report-status`). Packs are plain
  (undeltified) and byte-exact; shallow boundaries and `unshallow`
  transitions are tracked; non-fast-forward pushes are refused per ref
  with the exact reason `non-fast-forward`.
* **REST v3** — `/api/v3/` (root, meta, user), `/user/repos` with
  `per_page`/`page` and `Link: rel="next"`, repos CRUD, branches,
  `git/refs`, commits, contents (create/update/delete with sha checks),
  labels, issues (+ comments), pull requests (+ files, squash merge with
  3-way conflict detection). Every reply is JSON with a non-empty
  `X-GitHub-Request-Id`; missing/wrong bearer token is 401.
* **GraphQL** — `POST /api/graphql` with `{query, variables}`, batched
  arrays, `viewer`, `repository`, `node(id:)`, issues/pull-request
  connections with cursor pagination, the five mutations
  (`createIssue`, `closeIssue`, `createPullRequest`, `mergePullRequest`,
  `addComment`), `__type` introspection, and unknown-field errors.
  Global ids are opaque base64 (`Repository:<id>`, …), never the plain
  database id.
* **MCP** — `POST /mcp`, JSON-RPC 2.0. `server/discover` (modern era
  `2026-07-28`) and the legacy `initialize` handshake; `tools/list`
  offers `create_repo`, `get_repo`, `create_issue`, `list_issues`,
  `create_pull_request`, `merge_pull_request`, byte-identical across
  calls; unknown tools answer `-32602`, malformed JSON answers HTTP 400
  with `-32700`.

## Notes

* State is in memory only: restarting the forge starts with an empty
  forge. The trial harness seeds whatever it needs through the API.
* The viewer identity is fixed by the scenario: login `adaiversen`,
  id 1000. Any other owner is a 404.
* Only the bearer token in `MAKEFUN_SHOP_TOKEN` is accepted when set;
  with it unset, any non-empty bearer token is accepted (local testing).

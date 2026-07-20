# Published workspace design

> Status: **immutable publication core implemented.** The catalog, hash
> verification, publish/list/show/mount CLI, OpenClaw provisioning integration,
> and private materialized per-agent projections are live. Versioned streams,
> append logs, grant/revoke mutation, and delegation artifact references remain
> future work. This document retains those later phases as design rationale.

## 1. Goal

Each managed openclaw keeps mutable work private in its own workspace. When an
agent wants other claws to use an artifact, it explicitly publishes that work
through the clawie CLI. Published work appears automatically in each authorized
viewer agent's workspace as ordinary files.

The shared area must be:

- explicit: private work is never shared just because it exists;
- readable as a normal local filesystem, because all claws are on one host;
- clean: agents see a small friendly projection, not internal storage details;
- portable: the whole published workspace can be moved or backed up as one tree;
- durable: published entries are immutable, versioned, or append-only;
- concurrent: multiple agents can publish and read without corrupting state;
- permission-aware: an agent sees only the peers and publications it is allowed
  to see.

This is a local-host adaptation of the useful parts of Lattice A2A. Lattice uses
WebSockets, central task state, and artifact references so distributed agents can
collaborate. clawie can keep the same mental model while replacing bulk artifact
transport with a mounted, local, content-addressed publication store.

## 2. Current clawie facts

The existing managed openclaw workspace lives at:

```text
<agent-home>/.openclaw/workspace
```

That path is prepared in `clawie/_service_agents.py` when openclaw homes are
created. Current isolation is intentionally per Linux user: one agent should not
read another agent's home by default. `docs/runtime.md` describes this as
same-host, per-user isolation rather than container isolation.

Existing shared directories in `clawie/service.py` are for provider auth,
toolchains, and addon auth. They are not a general artifact workspace. The
delegation layer has task/message concepts, and `FileMailbox` exists in
`clawie/delegation.py`, but the normal delegation path is still task delivery,
not a published artifact filesystem.

The published workspace should therefore be a new subsystem. It should not relax
private workspace permissions and should not reuse credential-sharing paths.

## 3. Lattice lessons to keep

The useful Lattice ideas are protocol and product ideas, not the exact transport:

1. Directory first. Agents need a directory of visible peers, similar to Lattice
   AgentCards and directory snapshots.
2. Collaboration is contextual. A published artifact may belong to a context or
   task, and that identity should survive as metadata.
3. Artifacts are references. Lattice does not push large files through every A2A
   message. It stores task artifacts and lets messages refer to them.
4. Access is central. Lattice checks agent access and context membership before
   routing calls or broadcasting context events.
5. OpenClaw UX comes from tools and prompt state. Lattice's plugin injects A2A
   availability once, exposes `call_agent`, `get_task_result`, and
   `list_context_tasks`, and caches directory/context state locally.
6. Published content should be immutable or versioned. Lattice skill versions
   use content hashes and version rows; clawie can use the same pattern for
   published artifacts.
7. Workspace file operations must normalize paths. Lattice rejects path
   traversal, symlink escapes, special files, and oversized entries. clawie
   should apply the same rules when publishing from an agent workspace.

The piece to change is transport. Lattice has to work across machines and
deployments, so it uses WebSocket relay, database rows, and object storage-like
keys. clawie can do better on one machine: put bytes in a shared filesystem,
publish metadata atomically, and project authorized views into each agent's
workspace.

## 4. Core model

Terms:

- Private workspace: the agent-owned mutable openclaw workspace.
- Published workspace: the clawie-owned shared root containing manifests, blobs,
  catalogs, events, and generated views.
- Publication: one immutable published artifact tree.
- Stream: a human-friendly named sequence of publication versions, such as
  `alice/research-notes`.
- Append log: an append-only publication where new chunks may be added but old
  chunks are never rewritten.
- View: the per-viewer filesystem projection mounted into
  `<agent-home>/.openclaw/workspace/published`.
- Catalog: the indexed metadata store used by CLI commands and view generation.
- Event log: append-only JSONL notifications for global and per-agent changes.

OpenClaw should not need to know these internals. From the agent's perspective,
there is just a `published/` directory and a CLI that can publish new work.

## 5. Storage layout

Use one self-contained root:

```text
published-workspace/
  WORKSPACE.json
  catalog.sqlite
  catalog.lock

  blobs/
    sha256/
      ab/
        cd/
          <sha256>

  publications/
    pub_20260614T193012Z_alice_7f3a9c/
      manifest.json
      files/
        report.md
        data/results.json

  streams/
    alice/
      research-notes/
        stream.json
        versions/
          1 -> ../../../../publications/pub_...
          2 -> ../../../../publications/pub_...
        latest -> versions/2

  append/
    pub_20260614T193500Z_alice_91a812/
      manifest.json
      chunks/
        000001.jsonl
        000002.jsonl

  views/
    bob/
      _index.json
      _index.md
      alice/
        pub_20260614T193012Z_alice_7f3a9c/
          manifest.json
          files/
            report.md

  events/
    global.jsonl
    agents/
      bob.jsonl
      qa.jsonl

  snapshots/
    catalog-20260614T194000Z.json

  tmp/
```

Everything under this root is clawie-manager-only. `views/<viewer>/` is a
derived source for rebuilding a private materialized projection in that agent's
workspace; it is never exposed by relaxing or bypassing the root permissions.

The friendly in-agent layout should look like this:

```text
~/.openclaw/workspace/published/
  _index.md
  _index.json
  alice/
    research-notes/
      manifest.json
      files/
        report.md
        data/results.json
```

This keeps the user-facing workspace clean while preserving a portable,
content-addressed backing store.

## 6. Publication manifest

Each publication has a manifest like:

```json
{
  "schema": "clawie.published-workspace.v1",
  "publication_id": "pub_20260614T193012Z_alice_7f3a9c",
  "publisher_agent_id": "alice",
  "created_at": "2026-06-14T19:30:12Z",
  "title": "research notes",
  "mode": "immutable",
  "stream": {
    "name": "research-notes",
    "version": 2,
    "supersedes": "pub_20260614T182200Z_alice_02d41e"
  },
  "visibility": {
    "agents": ["bob", "qa"],
    "groups": []
  },
  "source": {
    "agent_workspace_relative_path": "notes/research",
    "source_digest": "sha256:<tree-hash>"
  },
  "context": {
    "context_id": "ctx_...",
    "task_id": "task_..."
  },
  "files": [
    {
      "path": "report.md",
      "sha256": "<sha256>",
      "size": 12345,
      "mode": "0644",
      "blob": "blobs/sha256/ab/cd/<sha256>",
      "content_type": "text/markdown"
    }
  ],
  "parents": []
}
```

The manifest is the contract. `files/` is the materialized tree for easy reading;
`blobs/` is the deduplicated immutable backing store. A verifier can recompute
file hashes and compare them with the manifest.

## 7. Publish transaction

Tentative CLI:

```text
clawie workspace publish PATH --agent alice --to bob,qa --title "research notes"
clawie workspace publish PATH --agent alice --to bob --stream research-notes
clawie workspace list --agent bob
clawie workspace show PUB_ID
clawie workspace grant PUB_ID --to qa
clawie workspace revoke PUB_ID --from bob
clawie workspace mount --agent bob
clawie workspace mount --all
clawie workspace verify PUB_ID
clawie workspace export --output published-workspace.zip
```

Publish flow:

1. Resolve the publishing agent and its openclaw workspace.
2. Require the source path to be inside that workspace unless an explicit
   manager-only override is used.
3. Normalize every path and reject path traversal, symlink escapes, special
   files, device files, sockets, and oversized entries.
4. Stage into `tmp/<publication-id>.staging/`.
5. Hash every file and copy it into `blobs/sha256/...` if absent.
6. Build a materialized read tree under the staging directory.
7. Write `manifest.json` last in the staging directory.
8. Atomically rename the staging directory into `publications/<publication-id>/`.
9. Commit the catalog row in SQLite.
10. Rebuild affected viewer projections under a lock.
11. Append events to `events/global.jsonl` and each authorized viewer's
    `events/agents/<viewer>.jsonl`.

If the same idempotency key and same tree digest are published again, the CLI can
return the existing publication. If the same stream version is attempted with
different content, return a conflict.

## 8. Mutability modes

Immutable publication:

- `publications/<pub-id>/` is never changed after commit.
- Grants can change the catalog and views, but not publication bytes.
- A changed artifact creates a new publication id.

Versioned stream:

- A stable stream name points at an ordered sequence of immutable publications.
- `latest` is a generated pointer to the newest authorized version.
- Updating a stream is an atomic catalog transaction plus pointer refresh.

Append-only publication:

- Existing chunks are never modified.
- New chunks are written to `tmp/`, fsynced, then renamed into
  `append/<pub-id>/chunks/<sequence>.jsonl`.
- The append manifest records sequence, hash, size, writer, and timestamp.
- Start with JSONL/text chunks only; arbitrary appendable directory semantics can
  come later if needed.

## 9. Concurrency

Use simple primitives that work on a local filesystem:

- SQLite in WAL mode for catalog metadata, with a busy timeout.
- `fcntl.flock` on `catalog.lock` for multi-file operations that must stay in
  sync, especially view rebuilds.
- Atomic directory rename for publication commit.
- Manifest written last, so a directory without a manifest is ignored or cleaned
  as incomplete.
- Per-stream lock for assigning the next version.
- Per-append-log lock for assigning the next chunk sequence.
- JSONL event writes opened with append mode and followed by fsync.
- `expected_version` or `expected_modified_at` where the CLI mutates catalog
  metadata, mirroring Lattice's workspace save concurrency check.

Readers should never need locks. They read committed directories and generated
views. The worst case during a refresh should be seeing the previous view until
the next atomic pointer update lands.

## 10. Visibility and permissions

There are two separate layers:

1. Catalog authorization: clawie decides which agents may see which
   publications and generates only those views.
2. OS enforcement: the filesystem must prevent direct reads around the view when
   agents are untrusted.

Preferred enforcement when clawie runs with enough privilege:

- root-owned published workspace;
- publication directories not world-readable;
- POSIX ACLs granting read/traverse access only to the publishing agent,
  authorized viewer Linux users, and the clawie manager user;
- generated views contain links only to publications the viewer may read.

Current enforcement (portable fallback):

- keep the canonical store and manager-side derived views under a `0700`
  manager-only root;
- materialize only authorized content into each agent's own private
  `workspace/published` projection;
- keep the internal publication store readable only by the clawie manager.

Avoid one broad Unix group containing all agents. That would recreate the class
of cross-agent access this feature is meant to avoid.

## 11. OpenClaw integration

On agent provisioning or reconcile, clawie should ensure:

```text
<agent-home>/.openclaw/workspace/published/
```

This is a regenerated, materialized projection. It deliberately does not use a
symlink through the manager's private state tree.

This belongs near the existing openclaw workspace preparation in
`clawie/_service_agents.py`. The mount is a derived projection, so it can be
recreated at any time from catalog metadata.

OpenClaw UX should be "magic" in the same way Lattice is:

- The first prompt/context should mention that shared artifacts from visible
  peers appear in `published/`.
- It should say that `published/` is disposable and non-authoritative: local
  edits are discarded on refresh, while canonical publications are immutable.
- It should point to the CLI command for publishing mutable private work.
- It should include the visible peer list, generated from the same policy that
  powers the views.

MVP does not require an OpenClaw plugin. A managed prompt snippet plus CLI is
enough. Later, a native tool can wrap the CLI as `publish_artifact`, but the
filesystem projection should remain the core primitive.

## 12. A2A integration shape

Published workspace should align with A2A metadata rather than replace A2A:

- A delegation result can include artifact references:

```json
{
  "kind": "artifact-ref",
  "publication_id": "pub_20260614T193012Z_alice_7f3a9c",
  "path": "files/report.md",
  "uri": "clawie://published/pub_20260614T193012Z_alice_7f3a9c/files/report.md"
}
```

- The receiver can also read the file directly at:

```text
published/alice/research-notes/files/report.md
```

- Publication manifests can carry `context_id`, `task_id`, and parent
  publication ids so the collaboration graph can be reconstructed.
- Events are local equivalents of Lattice context broadcasts:

```json
{"event":"published","publication_id":"pub_...","publisher":"alice","visible_to":["bob"],"created_at":"..."}
```

This lets clawie keep Lattice's clean A2A semantics while using the local
filesystem as the artifact transport.

## 13. Backup and restore

The published root is intentionally self-contained. Backup can include:

- `WORKSPACE.json`;
- `catalog.sqlite`;
- `publications/`;
- `append/`;
- `blobs/`;
- `streams/`;
- `events/`;
- periodic `snapshots/`.

`views/` and `tmp/` are derived and can be excluded. After restore:

```text
clawie workspace mount --all
clawie workspace verify --all
```

rebuilds projections and validates content hashes.

Export should follow the same discipline as Lattice workspace ZIP export:

- include a single top-level wrapper directory;
- reject symlink traversal and special files on import;
- skip platform junk;
- never import entries that escape the published root.

## 14. Implementation phases

Phase 1: filesystem and catalog library

- Add a `PublishedWorkspace` service module.
- Create root initialization and layout versioning.
- Add publication manifest generation, path validation, tree hashing, and CAS
  writes.
- Add SQLite catalog with WAL and simple event JSONL.

Phase 2: CLI surface

- Add `clawie workspace publish/list/show/mount/verify`.
- Start with immutable publications and generated views.
- Generate `_index.json` and `_index.md` per viewer.

Phase 3: openclaw provisioning integration

- During agent home preparation, create or refresh the `workspace/published`
  projection.
- Add a managed prompt snippet describing the published workspace and visible
  peers.
- Ensure the CLI is available inside the agent environment.

Phase 4: permission enforcement

- Add ACL-backed publication permissions when available.
- Add materialized per-viewer fallback when ACLs are not available.
- Add `grant` and `revoke` commands that update catalog, filesystem
  permissions, views, and event logs.

Phase 5: versioned streams and append-only logs

- Add stream version allocation and conflict handling.
- Add append-only JSONL chunk mode with per-log sequence locks.

Phase 6: A2A/delegation integration

- Let delegation results attach published artifact refs.
- Add context/task metadata to publications.
- Add commands to list publications by context and task.

## 15. Non-goals

- Do not make every private workspace shared.
- Do not send large artifact bytes through the delegation protocol.
- Do not use provider-auth or toolchain directories as artifact storage.
- Do not rely on prompt instructions as a permission boundary.
- Do not require a network relay for same-host artifact sharing.

## 16. Open questions

1. Should the default root be `/var/lib/clawie/published-workspace` for managed
   installs and `$CLAWIE_HOME/published-workspace` for user-mode installs?
2. Should view names prefer stable stream names, publication ids, or both?
3. Which ACL implementation should be the primary target on macOS and Linux?
4. Should `--to` accept only agent ids first, with groups added later, or should
   group visibility be part of the first implementation?
5. Should append-only mode be JSONL-only initially, or should it support generic
   file chunks from the start?

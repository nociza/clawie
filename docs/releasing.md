# Releasing

Clawie releases are built from an immutable GitHub release tag and published to
PyPI with short-lived OIDC credentials. Long-lived PyPI API tokens are not used.

## One-time repository setup

1. Create a protected GitHub environment named `pypi` with required maintainer
   approval and deployment restricted to protected `v*` tags.
2. In the PyPI `clawie` project, add a GitHub Trusted Publisher for repository
   `nociza/clawie`, workflow `release.yml`, environment `pypi`.
3. Protect `v*` tags and require the normal CI workflow before a GitHub release
   can be published.

## Release procedure

1. Update `pyproject.toml`, the lockfile, and version assertions together.
2. Run the full local gates, merge through protected CI, create annotated tag
   `vX.Y.Z`, and publish the matching GitHub release. Do **not** approve the
   waiting `pypi` environment yet.
3. When the workflow's build job passes, download its immutable
   `python-package-distributions` artifact. Run that exact wheel through the
   disposable target-host journey. The source home must contain real linked
   credentials and the host must provide the runtime's underlying package
   manager. The fixture installs the pinned runtime through the wheel, copies
   credentials into two private agent homes, starts both gateways, exercises
   nonce-bearing delivery and watchdog restart, verifies cleanup, and emits the
   artifact SHA-256 with the proof:

   ```bash
   sudo python3 scripts/production_verify_fixture.py \
     --wheel dist/clawie-X.Y.Z-py3-none-any.whl \
     --version X.Y.Z \
     --source-home /home/release-operator \
     --auth-source codex > production-proof.json
   ```

   A synthetic state row, placeholder auth file, skipped runtime-delivery
   exercise, failed fixture cleanup, or proof from a different wheel is not
   acceptable. Inspect the JSON and require both `result.status` to be `passed`
   and `cleanup.ok` to be `true`. Attach the proof to the GitHub release and
   confirm its `wheel_sha256` matches the downloaded artifact. The fixture
   creates its root-automation wrapper below root's trusted, owner-only home;
   a wrapper below `/tmp` or another group/world-writable ancestor must be
   rejected by production verification.

4. Only after the exact artifact proof passes, approve the `pypi` environment
   deployment. The release workflow independently
   verifies the tag/version match, repeats tests and security gates, builds and
   smoke-tests wheel plus sdist, then publishes only those verified artifacts.
5. Confirm `uv tool install clawie==X.Y.Z` installs the expected version and add
   the resulting PyPI URL to the release record.

The workflow fails closed on a tag/version mismatch, test failure, dependency
audit finding, lock drift, build failure, or missing artifact. Publishing is the
only job with `id-token: write`, and that job only downloads the verified
artifact and invokes the pinned PyPA publisher.

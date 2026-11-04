# Releasing Gary

Gary uses semantic versioning. A release is a git tag `vMAJOR.MINOR.PATCH` matching the
`version` in `pyproject.toml`, a GitHub Release with notes, and a versioned container image.

## Which number

| Bump | When | Example |
|---|---|---|
| **patch** | Fixes only: a rule applied wrongly, a crash, a message | `v1.4.2` |
| **minor** | New features or rules: a class move, a tool, a command | `v1.5.0` |
| **major** | Anything that breaks a running campaign: the saved-state format, a slash command removed or renamed, a setting the VM must change | `v2.0.0` |

## Cutting a release

**The usual way:** on GitHub, **Actions → release → Run workflow**, choose the bump. It:

1. runs the test suite (a red build is never released);
2. bumps `version` in `pyproject.toml`, commits it to `main`, tags `vX.Y.Z` and pushes;
3. creates the GitHub Release, with notes generated from the commits since the last tag;
4. starts the image build, which publishes `ghcr.io/magnentius/gary:X.Y.Z` and `:X.Y`.

**By hand:** set `version` in `pyproject.toml`, commit, then `git tag vX.Y.Z && git push origin
vX.Y.Z`. The workflow checks the tag matches `pyproject.toml` before creating the release.

## Deploying

Every push to `main` publishes `:latest`, which the VM pulls when it restarts — so a release
changes nothing on the VM by itself. A versioned tag is something to pin to, or roll back to:
point the VM's `startup.sh` at `ghcr.io/magnentius/gary:X.Y.Z` and reset it. Gary logs its
version at start-up (`Gary X.Y.Z starting`), so you can see which release is running.

# Releasing Gary

Gary's versions are dates: `YYYY.M.D`, in UTC, unpadded — `2026.11.4` — with `.N` added for
another release the same day (`2026.11.4.1`). A release is a git tag `v2026.11.4` matching the
`version` in `pyproject.toml`, a GitHub Release with notes, and a container image of that version.

Why dates, not semantic versioning: semver tells other people's code whether an upgrade breaks
them, and Gary has no such users — one bot, deployed by one table, changing most days. A date
says how old a build is and which build a bug appeared in, at a glance. Unpadded because
Python's packaging rewrites `2026.11.04` as `2026.11.4`, and the tag must match the version Gary
reports.

## Breaking changes

The number doesn't warn of them, so the release notes do. A change that breaks a running
campaign — the saved-state format, a slash command removed or renamed, a setting the VM must
change — gets a **⚠ Breaking** section at the top of its release, saying what to do before
deploying.

## Cutting a release

**The usual way:** on GitHub, **Actions → release → Run workflow**. Tick *breaking* (and say
what changes) if it applies. It:

1. runs the test suite (a red build is never released);
2. sets `version` in `pyproject.toml` to today, commits it to `main`, tags `vYYYY.M.D` and pushes;
3. creates the GitHub Release, with the ⚠ Breaking section if ticked, then notes generated from
   the commits since the last release;
4. starts the image build, which publishes `ghcr.io/magnentius/gary:YYYY.M.D`.

**By hand:** set `version` in `pyproject.toml`, commit, then `git tag vYYYY.M.D && git push origin
vYYYY.M.D`. The workflow checks the tag matches `pyproject.toml` before creating the release.

## Deploying

Every push to `main` publishes `:latest`, which the VM pulls when it restarts — so a release
changes nothing on the VM by itself. A dated image is something to pin to, or roll back to:
point the VM's `startup.sh` at `ghcr.io/magnentius/gary:YYYY.M.D` and reset it. Gary logs its
version at start-up (`Gary 2026.11.4 starting`), so you can see which release is running.

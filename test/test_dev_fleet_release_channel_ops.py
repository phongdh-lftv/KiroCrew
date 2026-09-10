"""Create/advance of a release-channel worktree, and how the fleet publishes it.

Two classes of defect are guarded here, both of which produce a tree that looks
healthy:

* **Resolving stale.** ``create`` / ``advance`` must fetch BEFORE resolving. The
  fleet's background refresher fetches only ``BASE_BRANCH``, so tags arrive on no
  other path — a resolve that ran first would pin a months-old release and report
  it as the channel tip.
* **Adopting a tree that is not ours.** ``release-channel-stable`` is a reserved
  name, and a user's own branch checkout under that name must never have its HEAD
  moved. Adoption requires the SHAPE (detached), never the name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    release_channel_pin,
    repository,
    runtime,
    worktree_ops,
)


class _Git:
    """Records every argv and answers the reads these ops make."""

    def __init__(
        self,
        *,
        tags=("v0.5.0",),
        head="old-oid",
        status="",
        fail=None,
        unmerged="0",
        behind="3",
    ):
        self.calls: list[list[str]] = []
        self._tags = list(tags)
        self._head = head
        self._status = status
        self._fail = fail or {}
        # `rev-list --count <tip>..HEAD`: commits this worktree holds that the lane
        # tip does not. "0" is the ordinary case -- a lane pin nobody committed on.
        self._unmerged = unmerged
        # `rev-list --count <head>..<tip>`: how far behind the lane tip the row is.
        self._behind = behind
        self.modes: list[str] = []

    async def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.modes.append(kw.get("mode", "standard"))
        for needle, result in self._fail.items():
            if needle in cmd:
                return result
        if "fetch" in cmd:
            return 0, "", ""
        if "tag" in cmd and "--list" in cmd:
            return 0, "\n".join(self._tags) + "\n", ""
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"  # detached
        if "status" in cmd:
            return 0, self._status, ""
        if "rev-parse" in cmd:
            if cmd[-1] == "HEAD":
                return 0, self._head + "\n", ""
            return 0, "tip-oid\n", ""
        if "worktree" in cmd and "add" in cmd:
            return 0, "", ""
        if "checkout" in cmd:
            return 0, "", ""
        if "rev-list" in cmd:
            # Two callers ask opposite questions of the same command, and the
            # RANGE DIRECTION is what tells them apart: `<tip>..HEAD` is "what
            # would be stranded" (the advance guard), `<head>..<tip>` is "how far
            # behind the lane tip" (the fleet row). Answering both with one number
            # is what made this fake agree with a guard that was not being tested.
            if cmd[-1].endswith("..HEAD"):
                return 0, self._unmerged + "\n", ""
            return 0, self._behind + "\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    def argv_with(self, needle: str) -> list[str] | None:
        for c in self.calls:
            if needle in c:
                return c
        return None

    def order(self, *needles: str) -> list[int]:
        """Index of the first call containing each needle."""
        out = []
        for needle in needles:
            out.append(next(i for i, c in enumerate(self.calls) if needle in c))
        return out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A primary checkout path whose sibling lane dirs genuinely do not exist."""
    # A DIRECTORY name, not prose: it mirrors the real primary checkout's own
    # folder, and `worktree_path` derives every lane dir as that folder's
    # sibling — so respelling it would stop the fixture matching the layout
    # under test.
    checkout = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    checkout.mkdir()
    monkeypatch.setattr(repository, "_repo", lambda: str(checkout))
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    # Each test owns its own lock, so a refusal in one cannot leak into the next.
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    return str(checkout)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_rejects_an_unknown_lane(repo, monkeypatch):
    got = await worktree_ops._release_channel_create("beta")
    assert got == {"ok": False, "error": "unknown release channel 'beta'"}


@pytest.mark.asyncio
async def test_create_refuses_when_the_worktree_already_exists(repo, monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", _found({"path": "/somewhere/release-channel-stable"})
    )
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "Advance" in got["error"]


@pytest.mark.asyncio
async def test_create_refuses_an_occupied_path_by_name(repo, monkeypatch):
    """Refuse and NAME the path rather than letting git talk about it.

    ``git worktree add`` fails on a non-empty path anyway, but its message is
    about a directory the operator may not know is involved.
    """
    Path(release_channel_pin.worktree_path(repo, "stable")).mkdir()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "already exists on disk" in got["error"]
    assert "release-channel-stable" in got["error"]


@pytest.mark.asyncio
async def test_create_adds_a_detached_worktree_at_the_resolved_tip(repo, monkeypatch):
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is True
    assert got["name"] == "release-channel-stable"
    assert got["version"] == "0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    argv = git.argv_with("add")
    assert argv is not None
    assert "--detach" in argv
    assert argv[-1] == "tip-oid"
    assert argv[-2] == release_channel_pin.worktree_path(repo, "stable")


@pytest.mark.asyncio
async def test_create_fetches_before_it_resolves(repo, monkeypatch):
    """Order is the guard against pinning a stale tag as the channel tip."""
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create("stable")
    fetch_at, tag_at = git.order("fetch", "--list")
    assert fetch_at < tag_at


@pytest.mark.asyncio
async def test_create_reports_a_failed_fetch_instead_of_resolving_locally(repo, monkeypatch):
    git = _Git(fail={"fetch": (1, "", "fatal: unable to access remote")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "cannot refresh release refs" in got["error"]
    assert git.argv_with("add") is None


@pytest.mark.asyncio
async def test_create_reports_a_failed_worktree_add(repo, monkeypatch):
    git = _Git(fail={"add": (128, "", "fatal: invalid reference")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "git worktree add failed" in got["error"]


# --------------------------------------------------------------------------
# advance
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_advance_refuses_a_worktree_that_is_on_a_branch(repo, monkeypatch):
    """The name guard: adoption requires the shape, never the name.

    Moving the HEAD of somebody's own ``release-channel-stable`` BRANCH checkout
    would silently abandon their work.
    """
    git = _Git()

    async def attached(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        return await git(cmd, **kw)

    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", attached)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "is on a branch, not detached" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_refuses_a_dirty_worktree(repo, monkeypatch):
    git = _Git(status=" M src/kiro_crew/__init__.py\n")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    monkeypatch.setattr(
        repository, "_dirt_report", _async_return(({"dirty_tracked": True}, " (1 modified)"))
    )
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "uncommitted changes" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_at_tip_is_a_success_not_an_error(repo, monkeypatch):
    """Nothing is wrong when the tree is already where it was asked to be.

    Refusing here would render a failure for the state the action was trying to
    reach.
    """
    git = _Git(head="tip-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["moved"] is False
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_refuses_to_strand_commits_the_tip_does_not_contain(repo, monkeypatch):
    """A CLEAN tree is not the same as nothing to lose.

    Committing is exactly what makes a worktree clean again, and a commit made on
    a detached HEAD belongs to no branch — so once HEAD moves it is reachable only
    from the reflog, and only until gc. `status --porcelain` cannot see that,
    which is why the dirty check is not the guard here.
    """
    git = _Git(head="old-oid", unmerged="2")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert got["unmerged_commits"] == 2
    assert "reflog" in got["error"]
    # The whole point: HEAD must not have moved.
    assert git.argv_with("checkout") is None
    # And the range asked is "what does the tip NOT contain", not the reverse.
    ranges = [c[-1] for c in git.calls if "rev-list" in c]
    assert ranges == ["tip-oid..HEAD"]


@pytest.mark.asyncio
async def test_advance_refuses_when_the_unmerged_probe_fails(repo, monkeypatch):
    """An unreadable answer is not permission to proceed.

    If `rev-list` cannot run, whether anything would be stranded is unknown, and
    checking out over an unknown is how the loss happens silently.
    """
    git = _Git(head="old-oid", fail={"rev-list": (128, "", "fatal: bad revision")})
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "cannot verify" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_detaches_onto_the_tip(repo, monkeypatch):
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["moved"] is True
    assert got["from_oid"] == "old-oid"
    argv = git.argv_with("checkout")
    assert argv is not None
    assert "--detach" in argv and argv[-1] == "tip-oid"


@pytest.mark.asyncio
async def test_advance_checks_out_without_credential_helpers(repo, monkeypatch):
    """The checkout runs repo-controlled content, so it uses the strict tier.

    Same boundary rebase uses: a checkout of worktree content must not run with
    the gateway's git credential helpers in its environment.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_advance("stable")
    idx = next(i for i, c in enumerate(git.calls) if "checkout" in c)
    assert git.modes[idx] == "strict"


@pytest.mark.asyncio
async def test_advance_reports_a_missing_worktree(repo, monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "not found" in got["error"]


# --------------------------------------------------------------------------
# fleet payload
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_publishes_a_lane_with_no_worktree_as_a_placeholder(repo, monkeypatch):
    """A lane the operator has not materialized is still listed.

    Without the placeholder there is nowhere on the page the feature is
    discoverable — there is no header control.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    rows = await fleet_state._release_channels([])
    by_lane = {r["lane"]: r for r in rows}
    assert set(by_lane) == set(release_channel_pin.RELEASE_CHANNELS)
    assert by_lane["stable"]["worktree"] is None
    assert by_lane["stable"]["version"] == "0.5.0"
    assert by_lane["stable"]["ref"] == "refs/tags/v0.5.0"
    assert by_lane["stable"]["name_taken_by_branch"] is False


@pytest.mark.asyncio
async def test_fleet_adopts_a_detached_worktree_and_counts_behind_the_lane_tip(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head="old-oid"))
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] == "release-channel-stable"
    assert stable["behind"] == 3
    assert stable["at_tip"] is False


@pytest.mark.asyncio
async def test_fleet_does_not_adopt_a_branch_checkout_that_shares_the_name(repo, monkeypatch):
    """The reserved name must not confer lane controls on somebody's branch."""

    async def attached(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", attached)
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] is None
    assert stable["name_taken_by_branch"] is True


@pytest.mark.asyncio
async def test_fleet_does_not_claim_a_branch_when_the_probe_could_not_read_head(repo, monkeypatch):
    """An unreadable HEAD is a THIRD state, not "on a branch".

    Collapsing it into ``name_taken_by_branch`` asserts a git fact about a tree
    nobody read, and because that flag also suppresses the lane's placeholder row,
    the lane would vanish from the page behind a fabricated explanation.
    """

    async def unreadable(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", "fatal: not a git repository"
        if "rev-parse" in cmd and cmd[-1] == "HEAD":
            return 1, "", "fatal: bad revision"
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", unreadable)
    monkeypatch.setattr(
        release_channel_pin,
        "worktree_state",
        _async_return({"head_oid": None, "at_tip": False, "behind": None, "detached": None}),
    )
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] is None
    assert stable["name_taken_by_branch"] is False
    assert stable["error"] and "could not be read" in stable["error"]


@pytest.mark.asyncio
async def test_fleet_publishes_the_worktree_basename_for_every_lane(repo, monkeypatch):
    """The frontend labels the not-yet-created row from this field.

    Re-deriving the name in the frontend would put the prefix rule on both sides
    of the boundary, where a change to WORKTREE_PREFIX desyncs the label from the
    directory that actually gets created.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    rows = await fleet_state._release_channels([])
    for row in rows:
        assert row["name"] == release_channel_pin.worktree_name(row["lane"])
    assert {r["name"] for r in rows} == {
        "release-channel-stable",
        "release-channel-insider",
        "release-channel-nightly",
    }


@pytest.mark.asyncio
async def test_fleet_reports_an_unresolvable_lane_without_dropping_it(repo, monkeypatch):
    """An absent key and a failed key are indistinguishable to the UI.

    "this repo has never cut a stable release" and "git could not be read" want
    different words on screen, so the lane stays in the list carrying its error.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git(tags=["v0.6.0-insider.6"]))
    rows = await fleet_state._release_channels([])
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["error"] and "no stable release tag" in stable["error"]
    assert stable["version"] is None
    assert stable["ref"] is None


@pytest.mark.asyncio
async def test_fleet_release_channels_never_raise(repo, monkeypatch):
    """This rides on the cached fleet snapshot; one bad lane must not blank it."""

    async def boom(*a, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(release_channel_pin, "resolve_all", boom)
    assert await fleet_state._release_channels([]) == []


@pytest.mark.asyncio
async def test_fleet_release_channels_empty_without_a_checkout(monkeypatch):
    monkeypatch.setattr(repository, "_repo", _raise(repository.RepoNotConfigured("no checkout")))
    assert await fleet_state._release_channels([]) == []


# --------------------------------------------------------------------------
# route validation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_route_rejects_an_unknown_lane_with_a_machine_readable_code():
    """The lane becomes a git ref and a directory name, so it is rejected, not
    sanitized — the same contract ``update_layout.set_release_channel`` keeps."""

    async def never_called(lane):  # pragma: no cover - must not run
        raise AssertionError("action ran on an invalid lane")

    resp = await http_api._lane_action(_FakeRequest({"lane": "../etc"}), never_called)
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_release_channel"


@pytest.mark.asyncio
async def test_route_rejects_a_missing_lane():
    async def never_called(lane):  # pragma: no cover - must not run
        raise AssertionError("action ran with no lane")

    resp = await http_api._lane_action(_FakeRequest({}), never_called)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_route_forwards_a_valid_lane():
    async def action(lane):
        return {"ok": True, "lane": lane}

    resp = await http_api._lane_action(_FakeRequest({"lane": "insider"}), action)
    assert resp.status == 200
    assert json.loads(resp.text)["lane"] == "insider"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body
        self.content_length = 1

    async def json(self):
        return self._body


def _found(entry: dict):
    async def _f(name):
        return entry, None

    return _f


def _missing():
    async def _f(name):
        return None, f"worktree not found: {name}"

    return _f


def _async_return(value):
    async def _f(*a, **kw):
        return value

    return _f


def _raise(exc):
    def _f(*a, **kw):
        raise exc

    return _f

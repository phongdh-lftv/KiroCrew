"""Release-channel resolution for Dev Fleet's per-lane worktrees.

The defect these tests exist to prevent is a SILENT one: a lane that resolves to
the wrong ref still produces a worktree that builds, boots as a pod and serves a
dashboard, so "stable" showing a prerelease looks exactly like success. Every
assertion below is therefore about the resolver's *answer*, not about whether it
ran.

Tag fixtures use this repository's real tag vocabulary (``v0.5.0``,
``v0.6.0-insider.6``) so a rename of the release workflow's tag shape shows up
here rather than in production.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.dev_fleet import release_channel_pin as rcp
from kiro_crew.apps.builtins.dev_fleet import repository, runtime

# Newest first, which is what `--sort=-creatordate` gives the resolver. The
# interleaving is the point: insider's tip is NEWER than stable's tip, so a
# resolver that ignored the lane filter and simply took the first line would
# return an insider tag for stable — and that is the real shape of this repo's
# tag history, not a contrived case.
_TAGS_NEWEST_FIRST = [
    "v0.6.0-insider.6",
    "v0.6.0-insider.5",
    "v0.5.0",
    "v0.5.0-insider.11",
    "v0.4.1",
]


def _fake_git(tags: list[str] | None = None, *, oids: dict[str, str] | None = None):
    """A git stand-in answering only what the resolver asks."""
    tags = _TAGS_NEWEST_FIRST if tags is None else tags
    oids = oids or {}

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "\n".join(tags) + "\n", ""
        if "rev-parse" in cmd:
            target = cmd[-1]
            if target in oids:
                return 0, oids[target] + "\n", ""
            # Deterministic stand-in oid derived from the ref, so assertions can
            # tie a returned oid back to the ref it was resolved from.
            return 0, f"oid-{target}\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    return fake_run


@pytest.fixture(autouse=True)
def _pinned_repo(monkeypatch):
    monkeypatch.setattr(repository, "_repo", lambda: "/fake/repo")
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lane", ["stable", "insider", "nightly"])
def test_worktree_name_carries_the_lane_under_the_shared_prefix(lane):
    """One naming rule, on the backend only.

    The fleet payload publishes this string per lane, so the frontend never
    rebuilds it — a second copy of the prefix rule is what would let a change to
    ``WORKTREE_PREFIX`` desync a row's label from the directory it names.
    """
    assert rcp.worktree_name(lane) == f"{rcp.WORKTREE_PREFIX}{lane}"
    assert rcp.worktree_name(lane).endswith(lane)


def test_worktree_name_is_a_valid_pod_identity():
    """The basename becomes ``kirocrew-pod@<name>.service``.

    A name that fails the pod name rule would surface as a pod that cannot be
    brought up — long after the worktree was created and built.
    """
    from kiro_crew.pod.runtime import _NAME_RE

    for lane in rcp.RELEASE_CHANNELS:
        assert _NAME_RE.match(rcp.worktree_name(lane)), lane


def test_worktree_path_is_a_sibling_of_the_primary_checkout():
    assert (
        rcp.worktree_path("/Users/me/Projects/KiroCrew", "stable")
        == "/Users/me/Projects/release-channel-stable"
    )


# --------------------------------------------------------------------------
# tag -> lane classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tag", "lane"),
    [
        ("v0.5.0", "stable"),
        ("v0.6.0-insider.6", "insider"),
        ("v0.6.0-rc.2", "insider"),
        ("v1.2.3-nightly.20260910", "nightly"),
    ],
)
def test_tag_lane_defers_to_the_shared_classifier(tag, lane):
    assert rcp.tag_lane(tag) == lane


@pytest.mark.parametrize("tag", ["main", "v1.2", "release-0.5.0", "v0.5.0.1", ""])
def test_tag_lane_rejects_non_release_tags(tag):
    assert rcp.tag_lane(tag) is None


def test_tag_lane_agrees_with_release_channel_module():
    """No second copy of the lane rule.

    A private rule here would drift from ``release_channel.channel`` the first
    time the release workflow adds a prerelease spelling, and the drift would be
    invisible: both answers are plausible strings.
    """
    from kiro_crew import release_channel

    for tag in _TAGS_NEWEST_FIRST:
        assert rcp.tag_lane(tag) == release_channel.channel(tag[1:])


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_stable_skips_newer_prerelease_tags(monkeypatch):
    """The core discriminator: insider's tip is newer, stable must not take it."""
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    assert got["version"] == "0.5.0"
    assert got["lane_check"] == "ok"


@pytest.mark.asyncio
async def test_resolve_insider_takes_newest_prerelease(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("insider")
    assert got["tag"] == "v0.6.0-insider.6"
    assert got["version"] == "0.6.0-insider.6"
    assert got["lane_check"] == "ok"


@pytest.mark.asyncio
async def test_resolve_nightly_uses_the_remote_branch_and_invents_no_version(monkeypatch):
    """Nightly is genuinely untagged, so there is no version to report.

    Synthesizing ``<base>-nightly.<stamp>`` here would put a version on screen
    that no artifact carries — the stamp is a property of a nightly BUILD, which
    this checkout is not.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("nightly")
    assert got["ok"] is True
    assert got["ref"] == "origin/main"
    assert got["tag"] is None
    assert got["version"] is None
    assert got["lane_check"] == "untagged"


@pytest.mark.asyncio
async def test_resolve_reports_oid_of_the_tagged_commit(monkeypatch):
    """Resolution must peel to a commit.

    An annotated tag's own object is not a commit, and handing a tag object to
    ``git worktree add`` / ``checkout --detach`` puts the worktree somewhere the
    behind-count cannot be computed from.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("stable")
    assert got["oid"] == "oid-refs/tags/v0.5.0^{commit}"


@pytest.mark.asyncio
async def test_resolve_rejects_an_unknown_lane(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git())
    got = await rcp.resolve("beta")
    assert got["ok"] is False
    assert "beta" in got["error"]


@pytest.mark.asyncio
async def test_resolve_reports_an_empty_lane_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.6.0-insider.6"]))
    got = await rcp.resolve("stable")
    assert got["ok"] is False
    assert "no stable release tag" in got["error"]


@pytest.mark.asyncio
async def test_resolve_skips_a_tag_that_will_not_resolve(monkeypatch):
    """One broken local ref must not report the whole lane as unpublished."""

    async def fake_run(cmd, **kw):
        if "tag" in cmd and "--list" in cmd:
            return 0, "v0.6.0\nv0.5.0\n", ""
        if "rev-parse" in cmd:
            if "v0.6.0^{commit}" in cmd[-1]:
                return 1, "", "bad object"
            return 0, f"oid-{cmd[-1]}\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert got["tag"] == "v0.5.0"


@pytest.mark.asyncio
async def test_resolve_flags_a_lane_check_mismatch_instead_of_shipping_it(monkeypatch):
    """The self-check earns its place only if a disagreement is reported.

    Forced by making the shape rule and the classifier disagree: the classifier
    is stubbed to answer ``nightly`` for a tag the shape rule accepted as
    stable. The resolver must hand back ``mismatch`` rather than presenting the
    ref as a clean stable pin.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _fake_git(tags=["v0.5.0"]))
    monkeypatch.setattr(rcp, "tag_lane", lambda tag: "stable")
    monkeypatch.setattr(rcp._release_channel, "channel", lambda v: "nightly")
    got = await rcp.resolve("stable")
    assert got["ok"] is True
    assert got["lane_check"] == "mismatch"


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_refs_is_additive_and_never_prunes_tags(monkeypatch):
    """The fetch must not be able to delete a tag the operator authored.

    Measured on git 2.54: the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, and a pruned tag ref is in no reflog. So this asserts the ABSENCE of
    both pruning flags: the cost of retraction coverage by this route is the
    operator's own tags, which is the worse trade.
    """
    seen: list[list[str]] = []

    async def fake_run(cmd, **kw):
        seen.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    assert await rcp.fetch_refs("/fake/repo") is None
    assert seen and "--tags" in seen[0]
    assert "--prune-tags" not in seen[0]
    assert "--prune" not in seen[0]


@pytest.mark.asyncio
async def test_fetch_refs_returns_a_redacted_error(monkeypatch):
    async def fake_run(cmd, **kw):
        return 1, "", "fatal: could not read Username for 'https://github.com'"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    err = await rcp.fetch_refs("/fake/repo")
    assert err and "fatal" in err


# --------------------------------------------------------------------------
# worktree position relative to the lane tip
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_state_counts_behind_the_channel_tip_not_main(monkeypatch):
    """``behind`` on a channel row means distance from the lane tip.

    The fleet's usual behind-count is against ``BASE_BRANCH``; on a release
    worktree that number is large and meaningless, because the worktree is not
    trying to track main.
    """
    calls: list[list[str]] = []

    async def fake_run(cmd, **kw):
        calls.append(cmd)
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "3\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid"})
    assert got["behind"] == 3
    assert got["at_tip"] is False
    assert got["detached"] is True
    ranges = [c[-1] for c in calls if "rev-list" in c]
    assert ranges == ["head-oid..tip-oid"]


@pytest.mark.asyncio
async def test_worktree_state_reports_at_tip_without_counting(monkeypatch):
    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", ""
        if "rev-parse" in cmd:
            return 0, "same-oid\n", ""
        raise AssertionError(f"should not have run: {cmd}")

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "same-oid"})
    assert got["at_tip"] is True
    assert got["behind"] == 0


@pytest.mark.asyncio
async def test_worktree_state_reports_an_attached_head_as_not_detached(monkeypatch):
    """The guard against adopting a coincidentally-named worktree.

    A user's own ``release-channel-stable`` branch checkout must not gain lane
    controls on the strength of its name; only a detached checkout at a resolved
    ref is a channel worktree.
    """

    async def fake_run(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        if "rev-parse" in cmd:
            return 0, "head-oid\n", ""
        if "rev-list" in cmd:
            return 0, "1\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(runtime, "_run_cmd", fake_run)
    got = await rcp.worktree_state("/wt", {"oid": "tip-oid"})
    assert got["detached"] is False

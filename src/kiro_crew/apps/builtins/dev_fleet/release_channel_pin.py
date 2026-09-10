"""Release-channel worktrees: one detached checkout per published lane.

Dev Fleet manages git checkouts, and a git checkout has no release channel —
``platform/update_capability.py`` says so outright: *"Only the wheel command
carries a channel. A git checkout follows its remote."* That is fine for the
install the user runs, and useless for the question this module answers: **what
did stable actually ship, and can I click through it right now?**

WHY A WORKTREE PER LANE, AND NOT A PIN ON THE PRIMARY CHECKOUT. Sync
fast-forwards the primary checkout (``git merge --ff-only``) and refuses to run
unless HEAD is literally :data:`repository.BASE_BRANCH`. A stable tag is
normally BEHIND main, so pinning that checkout to a lane could only work by
detaching its HEAD (which the sync guard rejects, and every ``origin/main``
comparison on the fleet row is then measuring against a ref the user did not
choose) or by resetting ``main`` backwards, which destroys work. So the lane
gets its own detached worktree instead: additive, non-destructive, and it lands
in the fleet as an ordinary row that pods and Make Live already know how to
drive.

WHAT THIS MODULE IS NOT. It never reads or writes ``$KIROCREW_HOME/channel``.
That file says which lane the user's real install FOLLOWS for updates; a pin
here says which git ref a worktree SITS ON. Coupling them would mean
materializing a stable worktree silently changed what the user's live install
downloads next — a blast radius nobody asked for. The only thing borrowed from
the update stack is vocabulary and validation.

Resolution is deliberately split from mutation: everything here is a read, so
the fleet snapshot can resolve every lane on its refresh path without any risk
of moving a worktree. The create/advance mutations live in ``worktree_ops``
beside the other worktree writers, because they take the same ``.git`` admin
lock those do.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew import release_channel as _release_channel
from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.platform.update_layout import RELEASE_CHANNELS

#: Basename prefix of a release-channel worktree. The basename becomes the fleet
#: row label (``fleet_state`` uses ``Path(path).name`` verbatim) AND the pod
#: identity (``kirocrew-pod@<name>.service``), so it is spelled in full rather
#: than abbreviated: ``release-channel-stable`` reads as what it is next to a
#: ``kirocrew-wt-<slug>`` feature worktree, and the missing ``kirocrew-wt-``
#: prefix is what visually separates the two groups with no extra chrome.
#:
#: Deliberately NOT ``channel-`` — bare "channel" already means four unrelated
#: things in this codebase (agent channels in ``kiro_crew/channel.py``, messaging
#: channels, notification channels, upload document channels).
WORKTREE_PREFIX = "release-channel-"

#: A tag naming a stable release: ``v1.2.3`` and nothing after it.
_STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

#: A tag naming a prerelease: ``v1.2.3-<suffix>``. The suffix is NOT interpreted
#: here — :func:`tag_lane` hands it to ``release_channel.channel``, which owns
#: the one rule for what a suffix means. Kept loose on purpose so a lane spelling
#: this module has never seen still resolves through that single classifier
#: rather than being dropped as unparseable.
_PRERELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-[0-9A-Za-z.\-]+$")


def worktree_name(lane: str) -> str:
    """The worktree basename for *lane*."""
    return f"{WORKTREE_PREFIX}{lane}"


def tag_lane(tag: str) -> str | None:
    """Which lane *tag* belongs to, or ``None`` if it is not a release tag.

    The lane rule is ``release_channel.channel``'s, not a second copy of it:
    this only decides whether the string is shaped like a release tag at all,
    then defers. That matters because the two prerelease spellings the release
    workflow publishes to the insider feed (``-insider.N`` and ``-rc.N``) are
    already reconciled there, and a private rule here would drift from it the
    first time a third spelling appears.
    """
    if not (_STABLE_TAG_RE.match(tag) or _PRERELEASE_TAG_RE.match(tag)):
        return None
    return _release_channel.channel(tag[1:])


def worktree_path(repo: str, lane: str) -> str:
    """Where *lane*'s worktree lives: a sibling of the primary checkout.

    Matches where the existing fleet already is — every ``kirocrew-wt-<slug>``
    worktree is a sibling of the primary checkout — so the new trees land in the
    directory the operator already associates with this repo instead of a second
    root they have to learn.
    """
    return str(Path(repo).parent / worktree_name(lane))


async def fetch_refs(repo: str, *, timeout: int = 120) -> str | None:
    """Refresh the remote-tracking ref AND the tags every lane resolves against.

    Returns ``None`` on success, else the error to report. Tags are what stable
    and insider resolve against and they arrive on no other path here — the
    fleet's background refresher fetches ``BASE_BRANCH`` only — so a resolve
    that skipped this would answer from whatever tags happened to be local,
    reporting a months-old release as the channel tip.

    ADDITIVE, and deliberately so: a tag deleted upstream (a retracted release)
    is NOT removed locally, so it stays resolvable as a channel tip until someone
    deletes it by hand. Pruning it is not available at this cost — measured on git
    2.54, the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, including ones the operator authored, and a pruned tag ref is not in
    any reflog. Trading an operator's own tags for retraction coverage is the
    worse bargain. Buying it properly means fetching release tags into a private
    namespace and resolving lanes there, which is a larger change than this.
    """
    remote = await repository._upstream_remote()
    rc, _out, err = await runtime._run_cmd(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--tags",
            remote,
            repository.BASE_BRANCH,
        ],
        timeout=timeout,
    )
    if rc != 0:
        return runtime._redact((err or "").strip())[:200] or f"git fetch {remote} --tags failed"
    return None


async def resolve(lane: str, *, repo: str | None = None) -> dict:
    """Resolve *lane* to the ref its channel most recently published.

    Read-only: never fetches (call :func:`fetch_refs` first when freshness
    matters) and never touches a worktree.

    Returns ``{"ok": True, lane, ref, tag, oid, version, lane_check}`` or
    ``{"ok": False, "error": ...}``.

    Lanes resolve differently and the asymmetry is real rather than an
    inconsistency to paper over: stable and insider are TAGGED by
    ``release.yml``, while ``nightly.yml`` builds from ``main`` HEAD on a
    schedule and tags nothing. So nightly resolves to the remote-tracking
    branch and reports ``tag: None`` / ``version: None`` instead of
    synthesizing the ``<base>-nightly.<stamp>`` string the workflow would have
    stamped — that stamp is a property of a BUILD, and inventing one here would
    put a version on screen that no artifact anywhere carries.

    Ordering is by tag creation date within the lane, because publication order
    is exactly what "channel tip" means: the tip is the last thing the lane
    shipped. Version-sorting instead would need this module to rank
    ``-insider.N`` against ``-rc.N``, a precedence nothing in the repo states,
    and would get it wrong silently.
    """
    if lane not in RELEASE_CHANNELS:
        return {
            "ok": False,
            "error": f"unknown release channel {lane!r} (expected one of {RELEASE_CHANNELS})",
        }
    if repo is None:
        repo = repository._repo()

    if lane == "nightly":
        remote = await repository._upstream_remote()
        ref = f"{remote}/{repository.BASE_BRANCH}"
        oid = await repository._git(repo, "rev-parse", f"{ref}^{{commit}}")
        if not oid:
            return {"ok": False, "error": f"cannot resolve {ref} (is the remote fetched?)"}
        return {
            "ok": True,
            "lane": lane,
            "ref": ref,
            "tag": None,
            "oid": oid,
            "version": None,
            # Not a failure: nightly is genuinely untagged, so there is no
            # version string to cross-check. Named so the UI can say "untagged"
            # rather than rendering a missing check as a failed one.
            "lane_check": "untagged",
        }

    listed = await list_release_tags(repo)
    if listed is None:
        return {"ok": False, "error": "cannot list tags (git tag failed)"}
    return await _resolve_tagged(lane, repo, listed)


async def list_release_tags(repo: str) -> list[str] | None:
    """Candidate release tags, newest published first. ``None`` if git failed.

    Split out so a caller resolving EVERY lane pays for one ``git tag`` instead
    of one per lane — the fleet snapshot resolves all three on its refresh path.
    """
    listed = await repository._git(repo, "tag", "--list", "v*", "--sort=-creatordate", timeout=20)
    if listed is None:
        return None
    return [ln.strip() for ln in listed.splitlines() if ln.strip()]


async def _resolve_tagged(lane: str, repo: str, listed: list[str]) -> dict:
    """Pick *lane*'s tip out of an already-listed, newest-first tag set."""
    for tag in listed:
        tag = tag.strip()
        if not tag or tag_lane(tag) != lane:
            continue
        oid = await repository._git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if not oid:
            # A listed tag that will not resolve is a broken local ref, not an
            # empty lane. Keep scanning rather than reporting the lane as
            # unpublished, which would hide a real release behind one bad ref.
            continue
        version = tag[1:]
        return {
            "ok": True,
            "lane": lane,
            "ref": f"refs/tags/{tag}",
            "tag": tag,
            "oid": oid,
            "version": version,
            # The resolver checking its own work. Selection filtered on
            # `tag_lane`, so a mismatch here means the tag SHAPE rule and the
            # version classifier disagree — and shipping a prerelease as
            # "stable" is precisely the silent error a lane pin must not make.
            # Surfaced as data instead of raising: the ref is still usable and
            # the operator is better served seeing which two answers conflict.
            "lane_check": "ok" if _release_channel.channel(version) == lane else "mismatch",
        }
    return {"ok": False, "error": f"no {lane} release tag found in this checkout"}


async def resolve_all(*, repo: str | None = None) -> dict[str, dict]:
    """Resolve every lane, listing tags once. Read-only, never fetches.

    A lane that fails to resolve is present in the mapping with its own
    ``{"ok": False, "error": ...}`` rather than omitted. An absent key and a
    failed key look identical to a caller iterating :data:`RELEASE_CHANNELS`, and
    the difference matters: "this repo has never cut a stable release" and "git
    could not be read" want different words on screen.
    """
    if repo is None:
        repo = repository._repo()
    out: dict[str, dict] = {}
    listed: list[str] | None = None
    listed_failed = False
    for lane in RELEASE_CHANNELS:
        if lane == "nightly":
            out[lane] = await resolve(lane, repo=repo)
            continue
        if listed is None and not listed_failed:
            listed = await list_release_tags(repo)
            listed_failed = listed is None
        if listed_failed:
            out[lane] = {"ok": False, "error": "cannot list tags (git tag failed)"}
            continue
        out[lane] = await _resolve_tagged(lane, repo, listed or [])
    return out


async def worktree_state(path: str, resolved: dict) -> dict:
    """Where the worktree at *path* sits relative to its resolved lane tip.

    ``at_tip`` / ``behind`` describe distance from the CHANNEL TIP, not from
    ``BASE_BRANCH`` — a release worktree is not trying to track main, so the
    fleet's usual behind-main count would be a large number that means nothing
    on this row.
    """
    out: dict = {"head_oid": None, "at_tip": False, "behind": None, "detached": None}
    head = await repository._git(path, "rev-parse", "HEAD")
    out["head_oid"] = head
    # `--quiet` exits non-zero on a detached HEAD, which `_git` reports as None.
    out["detached"] = (await repository._git(path, "symbolic-ref", "--quiet", "HEAD")) is None
    tip = resolved.get("oid")
    if not head or not tip:
        return out
    if head == tip:
        out["at_tip"] = True
        out["behind"] = 0
        return out
    count = await repository._git(path, "rev-list", "--count", f"{head}..{tip}", timeout=12)
    if count and count.isdigit():
        out["behind"] = int(count)
    return out


#: ``RELEASE_CHANNELS`` is deliberately ABSENT: it is re-exported for callers to
#: read as ``release_channel_pin.RELEASE_CHANNELS``, but its owner is
#: ``platform/update_layout``, and listing it here would make the Dev Fleet
#: compatibility facade claim ownership of a name from the update stack.
__all__ = [
    "WORKTREE_PREFIX",
    "fetch_refs",
    "list_release_tags",
    "resolve",
    "resolve_all",
    "tag_lane",
    "worktree_name",
    "worktree_path",
    "worktree_state",
]

"""Python half of the tool-call title conformance suite.

Reads the SAME fixture as ``website/src/utils/toolCallTitle.test.ts``. The two
implementations (``tool_call_title.py`` for the Slack / Discord renderers,
the TypeScript module for the dashboard) must agree on every case's ACTION and
its English TITLE, or a user sees one label in the dashboard and another in the
channel for the same call. The fixture is the contract; each side asserts it in
its own idiom. The direct unit tests below mirror the TS module's own.
"""

import json
from pathlib import Path

import pytest

from kiro_crew.tool_call_title import (
    classify_tool_call,
    derive_tool_call_title,
    format_raw_command,
    humanize_tool_name,
    mcp_identity_from_title,
    rel_display_path,
    short_display_path,
    tokenize_shell,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tool_call_titles.json"

# Fixture inputs use the TS camelCase field names; the Python API is snake_case.
_KEY_MAP = {
    "rawInput": "raw_input",
    "isShell": "is_shell",
    "toolName": "tool_name",
    "mcpServer": "mcp_server",
}

_CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _kwargs(case_input: dict) -> dict:
    return {_KEY_MAP.get(k, k): v for k, v in case_input.items()}


def test_fixture_has_corpus_shaped_cases():
    assert len(_CASES) > 50


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_fixture_conformance(case):
    inp = _kwargs(case["input"])
    expect = case["expect"]

    classified = classify_tool_call(**inp)
    if expect["action"] is None:
        assert classified is None
    else:
        assert classified is not None
        assert classified.action == expect["action"]
        assert classified.more == expect["more"]

    derived = derive_tool_call_title(**inp)
    assert derived.title == expect["title"]
    assert derived.kind == expect["kind"]
    assert derived.derived == expect["derived"]


# -- tokenize_shell ---------------------------------------------------------


def test_tokenize_splits_words_connectors_and_newlines():
    assert tokenize_shell("ls -la && cat a\ngrep x | wc") == [
        {"word": "ls"},
        {"word": "-la"},
        {"op": "&&"},
        {"word": "cat"},
        {"word": "a"},
        {"op": ";"},
        {"word": "grep"},
        {"word": "x"},
        {"op": "|"},
        {"word": "wc"},
    ]


def test_tokenize_joins_quoted_concatenations_into_one_word():
    assert tokenize_shell("rg -g\"*.py\" 'a b'") == [
        {"word": "rg"},
        {"word": "-g*.py"},
        {"word": "a b"},
    ]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("ls $HOME", id="$VAR"),
        pytest.param("echo $(date)", id="substitution"),
        pytest.param("ls > out.txt", id="redirect"),
        pytest.param("ls >> out.txt", id="append redirect"),
        pytest.param("wc < a.txt", id="input redirect"),
        pytest.param("cat <<'EOF'\nx\nEOF", id="heredoc"),
        pytest.param("ls *.py", id="glob"),
        pytest.param("cat ~/a", id="tilde"),
        pytest.param("(ls)", id="subshell"),
        pytest.param("sleep 1 &", id="background"),
        pytest.param("echo `date`", id="backtick"),
        pytest.param("echo 'abc", id="unterminated quote"),
        pytest.param('echo "$X"', id="expansion in double quotes"),
        pytest.param("ls # list", id="comment"),
    ],
)
def test_tokenize_rejects(script):
    assert tokenize_shell(script) is None


def test_tokenize_strips_stderr_silencing_redirects_only():
    assert tokenize_shell("ls x 2>/dev/null") == [{"word": "ls"}, {"word": "x"}]
    assert tokenize_shell("ls x 2>&1") == [{"word": "ls"}, {"word": "x"}]
    assert tokenize_shell("ls x 2>err.txt") is None


# -- paths -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("webview/src", "webview"),
        ("foo/src/", "foo"),
        ("packages/app/node_modules/", "app"),
        ("src", "src"),
        ("/a/b/c.ts", "c.ts"),
        (".", "."),
        ("C:\\x\\y.txt", "y.txt"),
    ],
)
def test_short_display_path(path, expected):
    assert short_display_path(path) == expected


def test_rel_display_path_relative_under_cwd_basename_at_cwd_parent_elsewhere():
    assert rel_display_path("/p/src/a.ts", "/p") == "src/a.ts"
    assert rel_display_path("/p", "/p") == "p"
    assert rel_display_path("/q/r/s/t.ts", "/p") == "s/t.ts"
    assert rel_display_path("/q/t.ts") == "q/t.ts"
    assert rel_display_path("~/n/t.md") == "~/n/t.md"


# -- MCP -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "server", "tool"),
    [
        ("@kirocrew-core/session_send", "kirocrew-core", "session_send"),
        ("Running: @kirocrew-dashboard/chat_folder_tree", "kirocrew-dashboard", "chat_folder_tree"),
        ("kirocrew-core___wait", "kirocrew-core", "wait"),
        ("mcp__github__list_prs", "github", "list_prs"),
    ],
)
def test_mcp_identity_from_title(title, server, tool):
    assert mcp_identity_from_title(title) == (server, tool)


@pytest.mark.parametrize("title", ["Running: ls -la", "Reading a.rs:1-20", "user@host/path"])
def test_mcp_identity_does_not_match_shell_title_or_plain_phrase(title):
    assert mcp_identity_from_title(title) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("session_send", "Session send"),
        ("artifact-folder-create", "Artifact folder create"),
        ("ChorusDocRead", "Chorus doc read"),
        ("wait", "Wait"),
    ],
)
def test_humanize_tool_name(name, expected):
    assert humanize_tool_name(name) == expected


# -- format_raw_command ----------------------------------------------------


def test_format_raw_command_keeps_short_single_line_verbatim():
    assert format_raw_command("ls -la") == "ls -la"


def test_format_raw_command_marks_further_lines_with_ellipsis():
    assert format_raw_command("cat > x <<EOF\nbody\nEOF") == "cat > x <<EOF …"


def test_format_raw_command_cuts_long_line_on_word_boundary():
    words = " ".join(f"word{i}" for i in range(30))
    out = format_raw_command(words)
    assert out.endswith("…")
    assert len(out) <= 81
    assert words.startswith(out[:-1])
    assert not out[:-1].endswith(" ")


def test_format_raw_command_collapses_internal_whitespace():
    assert format_raw_command("ls   -la\t src") == "ls -la src"


# -- derive_tool_call_title raw_title --------------------------------------


def test_raw_title_keeps_verbatim_command_for_shell_call():
    d = derive_tool_call_title(kind="execute", title="shell", raw_input={"command": "ls -la src"})
    assert d.raw_title == "ls -la src"
    assert d.title == "List files in src"


def test_raw_title_keeps_incoming_title_for_non_shell_call():
    d = derive_tool_call_title(
        kind="other", title="@kirocrew-core/session_send", raw_input={"target": "x"}
    )
    assert d.raw_title == "@kirocrew-core/session_send"

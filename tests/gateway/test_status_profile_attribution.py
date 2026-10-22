"""Profile attribution of a gateway launched through a runpy bootstrap — FORK-OWNED.

Upstream rewrites ``tests/gateway/test_status.py`` often, and a local test parked in that file
becomes a merge conflict that aborts ``hermes update`` (the updater merges ``origin/main`` into
the fork branch and stops on any conflict). Keeping the fork's coverage in its own file leaves
the fork with no delta on ``test_status.py``, so upstream can rewrite it freely and the file can
never conflict again.
"""

from __future__ import annotations

from pathlib import Path

from gateway import status
from gateway.status import profile_flag_value


def test_command_line_belongs_to_profile_runpy_bootstrap():
    """The Windows launcher runs the CLI as ``python -I -c "... sys.argv = ['...main.py',
    '--profile', 'ops', 'gateway', 'run']; ..."``: the profile flag is a quoted element
    INSIDE the -c source, so whitespace tokenization sees ``'--profile',`` (quote and
    comma attached) and the named-profile gateway reads as bare. Parse the embedded
    argv literal so the relaunched gateway is attributed to its profile (the updater's
    resume verification declared a healthy alpha gateway dead this way, exit 1)."""
    bootstrap = (
        'c:/hermes/tools/python.exe -I -c import sys, runpy; '
        'sys.path.insert(0, "c:/hermes/hermes-agent"); '
        "sys.argv = ['c:/hermes/hermes-agent/hermes_cli/main.py', '--profile', 'ops', 'gateway', 'run']; "
        "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    )
    home = Path("/fixture/profiles/ops")
    assert status._command_line_belongs_to_profile(bootstrap, home) is True
    # Token equality survives the parse: a sibling's value never matches (#-p ops vs ops-2).
    assert profile_flag_value(bootstrap) == "ops"
    sibling = (
        bootstrap.replace("', 'ops',", "', 'ops-2',")
    )
    assert not status._command_line_belongs_to_profile(sibling, home)
    assert profile_flag_value(sibling) == "ops-2"
    # Default stays the default: a bootstrap naming a profile is not the root home's.
    assert status._command_line_belongs_to_profile(bootstrap, Path("/opt/hermes-data")) is False


def test_windows_runpy_path_bootstrap_is_read_as_the_hermes_gateway():
    """A Windows path inside the bootstrap's source is a repr() literal, so every separator is
    DOUBLED ("C:\\\\app\\\\hermes_cli\\\\main.py"). The reader rewrites each backslash to a slash,
    which leaves "hermes_cli//main.py" — and the entrypoint check looks for
    "hermes_cli/main.py". Without collapsing the doubled separators the gateway is not
    recognized as a gateway at all, and the profile's gateway reads as dead."""
    source = (
        r"import sys, runpy; sys.path.insert(0, 'C:\\app\\hermes-agent'); "
        r"runpy.run_path('C:\\app\\hermes_cli\\main.py', run_name='__main__')"
    )
    command = (
        r"C:\\hermes\\tools\\python.exe -I -c " + source + r" gateway run --replace"
    )
    assert status.looks_like_gateway_command_line(command) is True

"""The fork's UTF-8 decode of a ``key_cmd`` helper's output — FORK-OWNED.

Upstream decodes the helper's stdout with the process locale encoding, which on
Windows is cp1252. A helper that prints a token carrying a non-ASCII character
writes UTF-8 bytes, so the locale decode turns the token into mojibake and the
corrupt value goes out as the provider credential. The fork decodes as UTF-8.

Kept out of upstream's ``tests/agent/test_command_token_source.py`` so the fork's
delta on that file stays zero — a changed line there is a merge conflict that
aborts ``hermes update``.
"""

from __future__ import annotations

import sys

from agent.command_token_source import _mint


def test_helper_output_is_decoded_as_utf8() -> None:
    """UTF-8 bytes from the helper survive as the token, not as mojibake."""
    # The command stays ASCII: the bytes are written by the helper, not carried
    # through the shell's own encoding.
    helper = "import sys; sys.stdout.buffer.write(b't\\xc3\\xb6k\\xc3\\xa9n')"
    token, ttl = _mint(f'{sys.executable} -c "{helper}"', "dbx")

    assert ttl is None
    assert token == "tökén"

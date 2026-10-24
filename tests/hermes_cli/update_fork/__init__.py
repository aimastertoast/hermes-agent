"""The fork's update-pipeline tests -- FORK-OWNED.

They live under ``tests/`` so upstream's ``pyproject.toml`` keeps its own
``testpaths`` untouched: the fork used to add ``hermes_cli/tests`` to that list,
which is a MODIFICATION of an upstream line in a file upstream rewrites daily --
a conflict magnet that aborted ``hermes update``.
"""

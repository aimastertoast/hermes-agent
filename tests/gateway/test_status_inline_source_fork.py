"""The fork's Windows path normalisation in the inline-bootstrap reader — FORK-OWNED.

``gateway/status_inline_source.py`` is upstream's. The fork's change (a Windows
path arrives with DOUBLED separators, because the caller rewrote every
separator to "/" on a source that already carried them) is a pure insertion
there, so this file pins the behaviour it produces.
"""

from __future__ import annotations

from gateway.status_inline_source import _bootstrap_entry


def _path_source(target: str) -> str:
    return (
        "import sys, runpy; sys.path.insert(0, '/x'); "
        f"runpy.run_path('{target}', run_name='__main__')"
    )


class TestWindowsPathCollapsing:
    def test_doubled_separators_are_collapsed(self):
        argv = _bootstrap_entry(_path_source("C://app//hermes_cli//main.py"), [])

        assert argv == ["C:/app/hermes_cli/main.py"]

    def test_a_normal_path_is_untouched(self):
        argv = _bootstrap_entry(_path_source("C:/app/hermes_cli/main.py"), [])

        assert argv == ["C:/app/hermes_cli/main.py"]

    def test_the_doubled_separators_of_the_arguments_are_collapsed_too(self):
        argv = _bootstrap_entry(_path_source("C://app//hermes_cli//main.py"), ["--config//x.yaml"])

        assert argv == ["C:/app/hermes_cli/main.py", "--config/x.yaml"]

    def test_a_module_source_is_not_collapsed(self):
        """Only the ``path`` kind is a filesystem path; a module name must survive verbatim."""
        source = (
            "import os, site, sys; sys.argv[0] = 'hermes'; "
            "site.addsitedir(os.environ['HERMES_SITE']); "
            "from hermes_cli.main import main; sys.exit(main())"
        )

        argv = _bootstrap_entry(source, ["--flag//value"])

        assert argv == ["-m", "hermes_cli.main", "--flag//value"]

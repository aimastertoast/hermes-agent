"""No module in the update path may CALL a name it never binds.

Background
----------
``update_cmd.py`` and its siblings are hand-frozen files: this repo patches them by
hand rather than tracking upstream, so a call can outlive the helper it referenced.
That happened on 2026-09-28 — the G3 commit (``51d513a3e6``) left
``update_cmd.py`` calling ``adopt_handed_off_gateway_resume()``, a helper that only
ever existed in a superseded hand-off lineage (it travelled in a ``GATEWAY_RESUME_ENV``
environment variable; the current ``update_handoff.py`` passes the resume token in a
file payload instead). The name is bound nowhere, so every Windows ``hermes update``
died at the first gateway-pause step with ``NameError``. Nothing was corrupted only
because that step runs before any git or config mutation.

A ``NameError`` on a frozen file is invisible to the unit tests: the crashing line sits
in the middle of ``_cmd_update_impl``, which no test calls end to end, and it is
*after* the function has already been entered. This test closes that gap statically.

How it works
------------
A name that is genuinely undefined is, by construction, bound nowhere in the file: not
as an assignment, import, def, class, parameter, comprehension target, ``with``/``except``
/``for`` target, ``global``/``nonlocal`` declaration, or walrus. Collecting every binding
and flagging ``Load``-context names that are in neither that set nor ``builtins`` needs no
scope analysis, so it has no false positives from conditional or nested bindings — only a
true undefined name can trip it.

The second check is the one that catches the real damage. A function whose *signature* was
narrowed by a refactor while its *body* kept the old locals reads perfectly well to the
caller and dies at run time on the first stale reference — so the test also runs
``symtable`` over the module and reports every name a function reads without binding it,
a parameter, or importing it. That is what caught the 2026-09-28 breakage: the update tail
referenced ``args``, ``opts``, ``is_fork``, ``pre_update_snapshot_id`` and six helpers that
no longer existed, and no test in the repo executes ``_cmd_update_impl`` end to end.
"""

import ast
import builtins
import symtable
from pathlib import Path

import pytest


# The hand-frozen update-path modules. Each has been patched by hand in this repo and each
# is reachable from `hermes update`, so a stale call in any of them breaks the live update.
FROZEN_UPDATE_MODULES = (
    "hermes_cli/update_cmd.py",
    "hermes_cli/update_verification.py",
    "hermes_cli/update_orchestrator.py",
    "hermes_cli/update_handoff.py",
    "hermes_cli/update_receipt.py",
)

_BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


def _bound_names(tree: ast.AST) -> set:
    """Every name the module binds anywhere, at any scope."""
    bound: set = set()

    def add(target):
        if isinstance(target, ast.Name):
            bound.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                add(element)
        elif isinstance(target, ast.Starred):
            add(target.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            arguments = getattr(node, "args", None)
            if arguments is not None:
                for group in (arguments.posonlyargs, arguments.args, arguments.kwonlyargs):
                    bound.update(argument.arg for argument in group)
                for extra in (arguments.vararg, arguments.kwarg):
                    if extra is not None:
                        bound.add(extra.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _called_names(tree: ast.AST) -> set:
    """Names read as a bare function call target — the shape that raises ``NameError``."""
    called: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def _module_path(relative: str) -> Path:
    import hermes_cli
    return Path(hermes_cli.__file__).parent.parent / relative


def _is_bound(symbol) -> bool:
    """``symtable`` reports a function-local ``import`` as IMPORTED, not ASSIGNED."""
    return (symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()
            or symbol.is_namespace() or symbol.is_free())


def _leaked_free_variables(relative: str, source: str) -> dict:
    """Per top-level function: names it reads that it neither binds nor inherits.

    ``symtable`` does the scope analysis CPython itself uses, so a name bound by a
    nested ``def``'s parameter, a comprehension target, an ``except ... as`` clause, a
    ``with``/``for`` target or a function-local import is correctly treated as bound.
    """
    module = symtable.symtable(source, relative, "exec")
    module_names = {symbol.get_name() for symbol in module.get_symbols()}
    allowed = module_names | _BUILTINS

    leaked = {}
    for scope in module.get_children():
        missing = sorted(
            symbol.get_name() for symbol in scope.get_symbols()
            if symbol.is_referenced() and not _is_bound(symbol)
            and symbol.get_name() not in allowed
        )
        if missing:
            leaked[scope.get_name()] = missing
    return leaked


@pytest.mark.parametrize("relative", FROZEN_UPDATE_MODULES)
def test_frozen_update_module_calls_no_undefined_name(relative):
    """Every bare call target must be bound in the file or be a builtin."""
    source = _module_path(relative).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)

    undefined = _called_names(tree) - _bound_names(tree) - _BUILTINS

    assert not undefined, (
        f"{relative} calls {sorted(undefined)!r}, which the module never binds. "
        "On the live update path this raises NameError mid-run — usually because a "
        "helper was renamed or dropped in a different lineage, or because a call "
        "referenced a symbol that only ever existed behind a superseded mechanism. "
        "Check the name against the rest of the update package before adding it back."
    )


@pytest.mark.parametrize("relative", FROZEN_UPDATE_MODULES)
def test_frozen_update_module_reads_no_unbound_local(relative):
    """No function may read a name it neither binds, imports, nor takes as a parameter.

    This is the check that catches a refactor that narrowed a signature without
    narrowing its body: the caller is happy, the file parses, and the update dies on the
    first stale reference at run time. Repair it by restoring the parameter or by routing
    the work through whichever module owns it now — not by inventing a module-level
    placeholder, which silently turns a crash into a wrong value.
    """
    relative_path = _module_path(relative)
    source = relative_path.read_text(encoding="utf-8")

    leaked = _leaked_free_variables(relative, source)

    assert not leaked, (
        f"{relative} reads names its own scope never binds:\\n"
        + "\\n".join(f"  {name}() -> {names}" for name, names in sorted(leaked.items()))
        + "\\n\\nEach of these raises NameError the moment that line executes."
    )


def test_the_known_dead_handoff_helper_is_not_referenced():
    """Pins the specific regression this file was written for.

    ``adopt_handed_off_gateway_resume`` lived in a superseded hand-off lineage that
    shipped the resume token in the ``GATEWAY_RESUME_ENV`` environment variable. The
    current hand-off is a FILE payload (``update_handoff.write_handoff``), read by the
    process that ``update_completion.run_completion`` spawns on the pulled tree. If a
    future merge reintroduces the call, the update crashes before it mutates anything —
    see the module docstring.
    """
    import hermes_cli.update_cmd as update_cmd

    source = Path(update_cmd.__file__).read_text(encoding="utf-8")
    assert "adopt_handed_off_gateway_resume" not in source

    # And the helper really is absent from the hand-off module it once lived in.
    import hermes_cli.update_handoff as update_handoff

    assert not hasattr(update_handoff, "adopt_handed_off_gateway_resume")

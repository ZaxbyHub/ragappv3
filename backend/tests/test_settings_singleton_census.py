"""Acceptance check C11/AC11 (issue #987): settings-singleton census.

A NEW-SURFACE guard: this file does not exist at the base tree (base leg
errors with 'file or directory not found' by construction) and must PASS at
the fixed tree.

``app.config.settings`` is a process-wide singleton; a test that assigns
``settings.<field> = ...`` without restoring it poisons every later test in
the same pytest process (issue #987: the o04c curator test leaked six attrs
and flipped tests/test_settings_curator.py from 422 to 200). This census
enumerates every bare ``settings.<field> = ...`` assignment (ast ``Assign``
whose target is an Attribute on the name ``settings``) across
``backend/tests/**/*.py`` and FAILS when a hit lacks restore evidence and is
not on the documented SCOPED list below.

Heuristic (kept deliberately simple and generous toward legit patterns, so
that only genuinely un-restored assignments surface):

* NOT the singleton — SKIPPED: assignments inside a function that binds the
  NAME ``settings`` locally (``settings = MagicMock()``,
  ``settings = Settings()`` fresh instances, a ``settings`` parameter). The
  codebase's mock-builder helpers do exactly this; their assignments target
  a local object, never the app singleton.
* RESTORED — evidence, checked module-wide: the field name appears as a
  STRING LITERAL anywhere in the same test module (the universal shape of
  snapshot/restore helpers: ``_FlagSnapshot`` field tuples,
  ``{f: getattr(settings, f) for f in ("<field>", ...)}`` snapshot dicts,
  ``patch.object(settings, "<field>")``, ``setattr`` restore loops), OR the
  module contains a READ of ``settings.<field>`` (ast Attribute in Load
  context — the ``orig = settings.<f>`` snapshot half of an explicit
  save/restore pair). Module-wide (rather than same-class) evidence is a
  documented generosity: this codebase keeps its snapshot/restore helpers at
  module level (``_FlagSnapshot``, ``_snapshot_flags``/``_restore_flags``)
  or in base classes, away from the assigning test class.
* Anything else is a HIT and must be on the SCOPED list with a reason.

Consequence of the module-wide generosity: a second class in an otherwise
clean module could piggyback on another class's restore evidence. That is
accepted — the o04c fields themselves are pinned precisely by the C9/C10
guard; this census is the tripwire for NEW un-restored assignments.
"""

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Documented exceptions — every entry needs a reason. Entries found and
# reviewed while calibrating the census at the base tree (77ffda7a). The
# known o04c polluter fields are deliberately NOT here: the #987 fix
# restores them, and leaving them listed would re-green a regression.
SCOPED: dict[tuple[str, str, str], str] = {
    # (file stem, class-or-scope, field): reason
    # Frozen issue-#513 standalone-script enclave: module-level data_dir
    # seeding inside frozen check scripts (blob-id manifest), isolated by
    # that directory's own conftest (env/module-graph snapshot around
    # collection). Out of the #987 fix surface; not modifiable.
    ("test_c14_maintenance_upload_orphan", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c15_stale_generation_retry", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c18_asset_bytes_sweeper", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c19_vault_delete_rollback", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c20_retry_accounting", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c23_bulk_delete_race", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c26_finalization_convergence", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    ("test_c6_batch_mapping_desync", "<module>", "data_dir"): (
        "frozen issue513_checks standalone-script enclave"
    ),
    # Restored via DYNAMIC field families: setUp snapshots every
    # INFRA_REDACTED_FIELDS member ending in _url/_model and tearDown /
    # addCleanup restores — the field names arrive from an imported constant
    # list, so no literal/read evidence exists for the heuristic to see.
    ("test_issue660_connection_redaction", "_Issue660Harness", "reranker_url"): (
        "restored via dynamic INFRA_REDACTED_FIELDS family snapshot/restore"
    ),
    ("test_issue660_nonadmin_shape", "TestIssue660NonAdminShape", "ollama_embedding_url"): (
        "restored via dynamic INFRA_REDACTED_FIELDS family snapshot/restore"
    ),
    ("test_issue660_nonadmin_shape", "TestIssue660NonAdminShape", "ollama_chat_url"): (
        "restored via dynamic INFRA_REDACTED_FIELDS family snapshot/restore"
    ),
    ("test_issue660_nonadmin_shape", "TestIssue660NonAdminShape", "reranker_url"): (
        "restored via dynamic INFRA_REDACTED_FIELDS family snapshot/restore"
    ),
    ("test_issue660_nonadmin_shape", "TestIssue660NonAdminShape", "reranker_model"): (
        "restored via dynamic INFRA_REDACTED_FIELDS family snapshot/restore"
    ),
    # Restored by the module's _settings_values(**overrides) context manager
    # (setattr-snapshot/restore of its kwargs); the assignments sit inside
    # its with-block, and kwarg names are invisible to the literal heuristic.
    ("test_unconfigured_models", "TestIngestionDetachOnClearedPair", "instant_chat_url"): (
        "restored by the _settings_values(**overrides) context manager"
    ),
    ("test_unconfigured_models", "TestIngestionDetachOnClearedPair", "ollama_chat_url"): (
        "restored by the _settings_values(**overrides) context manager"
    ),
    # NOTE: ("test_wiki_curator", "TestWikiCurator",
    # "wiki_llm_curator_concurrency") was a REAL un-restored assignment found
    # during calibration (leaked wiki_llm_curator_concurrency=2 to the
    # process). It is deliberately NOT scoped: the #987 fix restores it
    # (snapshot list extension), so the census fails until that restore
    # lands. Do NOT add new SCOPED entries of this kind.
}


class _Hit:
    def __init__(self, file: str, scope: str, field: str, lineno: int):
        self.file = file
        self.scope = scope
        self.field = field
        self.lineno = lineno

    def key(self) -> tuple[str, str, str]:
        return (self.file, self.scope, self.field)

    def __str__(self) -> str:
        return f"{self.file}:{self.scope}:{self.field} (line {self.lineno})"


def _module_evidence(tree: ast.Module) -> set[str]:
    """Fields with module-wide restore evidence: string literals equal to the
    field name, or Load-context reads of settings.<field>."""
    evidenced: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        ):
            evidenced.add(node.value)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
            and isinstance(node.ctx, ast.Load)
        ):
            evidenced.add(node.attr)
    return evidenced


def _shadows_settings(func: ast.AST) -> bool:
    """True when the function binds the NAME ``settings`` locally, so
    ``settings.<f> = ...`` inside it targets a local object (MagicMock /
    fresh Settings), not the app singleton."""
    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in (
            list(func.args.args)
            + list(func.args.posonlyargs)
            + list(func.args.kwonlyargs)
        ):
            if arg.arg == "settings":
                return True
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "settings":
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ) and node.target.id == "settings":
            return True
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name) and (
                    item.optional_vars.id == "settings"
                ):
                    return True
        if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(
            node.target, ast.Name
        ) and node.target.id == "settings":
            return True
    return False


def _scan_tree(tree: ast.Module, path_stem: str, evidenced: set) -> tuple[list, int]:
    """Count settings-singleton mutations in ONE tree, all three forms.

    Extracted from ``_census`` (PR #996 review round) so the scanner is
    directly unit-testable: the original loop walked only ``ast.Assign``,
    leaving annotated (``settings.x: T = v``) and augmented
    (``settings.x += v``) singleton mutations invisible to the guard —
    both leak onto the process-wide singleton exactly like a plain store.
    A bare annotation ``settings.x: T`` (no value) assigns nothing and is
    not counted.
    """
    hits: list[_Hit] = []
    total = 0

    parent_of: dict[int, ast.AST] = {}

    def _link(parent):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent
            _link(child)

    _link(tree)

    def _nearest(node: ast.AST, kinds):
        cur = parent_of.get(id(node))
        while cur is not None:
            if isinstance(cur, kinds):
                return cur
            cur = parent_of.get(id(cur))
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "settings"
                and isinstance(target.ctx, ast.Store)
            ):
                continue
            field = target.attr
            enclosing_func = _nearest(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
            if enclosing_func is not None and _shadows_settings(
                enclosing_func
            ):
                continue  # local mock/fresh instance, not the singleton
            total += 1
            if field in evidenced:
                continue  # module-wide snapshot/restore evidence exists
            cls = _nearest(node, ast.ClassDef)
            scope = cls.name if cls is not None else "<module>"
            hits.append(_Hit(path_stem, scope, field, node.lineno))
    return hits, total


def _census() -> tuple[list[_Hit], int]:
    hits: list[_Hit] = []
    total = 0
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        file_hits, file_total = _scan_tree(
            tree, path.stem, _module_evidence(tree)
        )
        hits.extend(file_hits)
        total += file_total
    return hits, total


def test_no_unrestored_settings_singleton_assignments():
    hits, total = _census()
    # Population floor: the census must keep seeing real singleton
    # assignments; an empty scan would green the guard on a parser drift.
    assert total >= 50, (
        f"census population collapsed to {total} singleton assignment sites "
        "(expected >= 50 across backend/tests) — the scanner is no longer "
        "seeing the code; check the AST filters"
    )
    unexpected = [h for h in hits if h.key() not in SCOPED]
    assert not unexpected, (
        f"{len(unexpected)} of {total} singleton settings.<field> "
        "assignments in backend/tests lack module-wide snapshot/restore "
        "evidence and are not on the SCOPED list (issue #987 AC11 — "
        "settings-singleton test pollution): "
        + "; ".join(str(h) for h in unexpected)
    )


def test_annotated_and_augmented_singleton_assignments_are_censused():
    """PR #996 review round: the scanner counts AnnAssign/AugAssign
    singleton mutations, not just plain ``ast.Assign``.

    An annotated ``settings.x: T = v`` or augmented ``settings.x += v``
    leaks onto the process-wide singleton exactly like a plain store but
    was invisible to the original census walk (hermes finding HF-2); this
    pins the closed detection hole against a synthetic in-memory module
    (no fixture file planted in the real tests tree).
    """
    src = (
        "class TestThing:\n"
        "    def setUp(self):\n"
        "        settings.wiki_llm_curator_mode: str = 'active_if_verified'\n"
        "        settings.wiki_llm_curator_concurrency += 1\n"
        "        settings.wiki_llm_curator_url = 'https://api.example.com'\n"
    )
    hits, total = _scan_tree(ast.parse(src), "synthetic_probe", set())
    assert total == 3, (
        "all three singleton mutation forms must be censused "
        f"(plain/annotated/augmented); got total={total}"
    )
    assert {h.field for h in hits} == {
        "wiki_llm_curator_mode",
        "wiki_llm_curator_concurrency",
        "wiki_llm_curator_url",
    }
    # A bare annotation without a value assigns nothing and is not counted.
    bare = (
        "class TestThing:\n"
        "    def setUp(self):\n"
        "        settings.wiki_llm_curator_mode: str\n"
    )
    _, bare_total = _scan_tree(ast.parse(bare), "synthetic_probe", set())
    assert bare_total == 0

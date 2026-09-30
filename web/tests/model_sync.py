"""Compares the SQLAlchemy model definitions in web/app/models.py and
worker/app/models.py without importing either.

The two services share one database but are not a shared Python package, so
every schema change has to be mirrored by hand (see CLAUDE.md). Importing both
modules in one process to compare them does not work: each does
`from app.db import Base` against its own `app` package, so under one sys.path
only one of them resolves, and mapping both onto a single declarative Base
raises "Table 'storage_locations' is already defined".

So this reads both files with `ast` instead. No imports, no database, no
SQLAlchemy - which also means it runs in the default dependency-free suite.

The worker deliberately mirrors only the subset of tables and columns it
touches, so `compare_model` reports a column missing from the worker as
`worker_missing` and leaves it to the caller to decide whether that matters.
A column present in both but with a different type is always a real defect.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_MODELS = REPO_ROOT / "web" / "app" / "models.py"
WORKER_MODELS = REPO_ROOT / "worker" / "app" / "models.py"


@dataclass
class Column:
    name: str
    # The SQLAlchemy type as written, e.g. "BigInteger", "String", "Boolean".
    # None when mapped_column() was called with no positional type, in which
    # case SQLAlchemy infers it from the Mapped[...] annotation.
    sa_type: str | None
    annotation: str


@dataclass
class ModelDiff:
    table: str
    worker_missing: list[str] = field(default_factory=list)
    web_missing: list[str] = field(default_factory=list)
    type_mismatch: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.worker_missing or self.web_missing or self.type_mismatch)

    def __str__(self) -> str:
        parts = []
        if self.type_mismatch:
            parts.append(f"type mismatch: {', '.join(self.type_mismatch)}")
        if self.worker_missing:
            parts.append(f"missing from worker: {', '.join(self.worker_missing)}")
        if self.web_missing:
            parts.append(f"missing from web: {', '.join(self.web_missing)}")
        return f"{self.table}: " + ("; ".join(parts) if parts else "in sync")


def _first_type_arg(call: ast.Call) -> str | None:
    """The type named in mapped_column(...), e.g. BigInteger in
    mapped_column(BigInteger, default=0). Handles both `BigInteger` and
    `String(64)` forms. Returns None when no positional type was given."""
    for arg in call.args:
        if isinstance(arg, ast.Name):
            return arg.id
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            return arg.func.id
        # ForeignKey("x.y") and similar are positional too but are not the
        # column's type; skip them and keep looking.
        if isinstance(arg, ast.Call):
            continue
    return None


def parse_models(path: Path) -> dict[str, dict[str, Column]]:
    """Maps __tablename__ -> {column name -> Column} for every model class in
    the file. Classes without a __tablename__ are ignored."""
    tree = ast.parse(path.read_text(), filename=str(path))
    models: dict[str, dict[str, Column]] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        table_name: str | None = None
        columns: dict[str, Column] = {}

        for item in node.body:
            # __tablename__ = "storage_locations"
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__tablename__"
                        and isinstance(item.value, ast.Constant)
                    ):
                        table_name = item.value.value
            # name: Mapped[str] = mapped_column(String, ...)
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if not (
                    isinstance(item.value, ast.Call)
                    and isinstance(item.value.func, ast.Name)
                    and item.value.func.id == "mapped_column"
                ):
                    continue
                columns[item.target.id] = Column(
                    name=item.target.id,
                    sa_type=_first_type_arg(item.value),
                    annotation=ast.unparse(item.annotation),
                )

        if table_name:
            models[table_name] = columns

    return models


def compare_model(table: str) -> ModelDiff:
    """Compares one table's columns across the two model files."""
    web = parse_models(WEB_MODELS)
    worker = parse_models(WORKER_MODELS)

    if table not in web:
        raise AssertionError(f"{table} is not defined in {WEB_MODELS}")
    if table not in worker:
        raise AssertionError(f"{table} is not defined in {WORKER_MODELS}")

    web_cols, worker_cols = web[table], worker[table]
    diff = ModelDiff(table=table)

    for name in web_cols:
        if name not in worker_cols:
            diff.worker_missing.append(name)
    for name in worker_cols:
        if name not in web_cols:
            diff.web_missing.append(name)

    for name in sorted(set(web_cols) & set(worker_cols)):
        w, k = web_cols[name], worker_cols[name]
        # Only compare when both name a type; an inferred type on one side and
        # an explicit one on the other is not necessarily a conflict.
        if w.sa_type and k.sa_type and w.sa_type != k.sa_type:
            diff.type_mismatch.append(f"{name} (web={w.sa_type}, worker={k.sa_type})")

    return diff


def assert_columns_mirrored(table: str, columns: list[str]) -> None:
    """Asserts that each named column exists in both files with the same type.

    This is the check a step's test should use: it says "the columns I just
    added are mirrored" without failing on the many columns the worker has
    always, deliberately, left out.
    """
    diff = compare_model(table)
    missing = [c for c in columns if c in diff.worker_missing]
    if missing:
        raise AssertionError(
            f"{table}: {', '.join(missing)} added to web/app/models.py but not "
            f"worker/app/models.py - the two must be mirrored by hand"
        )
    mismatched = [m for m in diff.type_mismatch if m.split(" ")[0] in columns]
    if mismatched:
        raise AssertionError(f"{table}: {'; '.join(mismatched)}")

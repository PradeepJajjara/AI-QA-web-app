"""Load CSV/Excel uploads into DuckDB tables and extract schema metadata.

All limits here are assumptions — the brief sets none. They are stated in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

MAX_FILES = 5
MAX_FILE_MB = 50
SAMPLE_VALUES = 12  # low-cardinality columns show every value to the model

CSV_EXT = {".csv", ".tsv", ".txt"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}


class LoadError(Exception):
    """User-facing load failure. Message is shown verbatim in the UI."""


@dataclass
class ColumnInfo:
    name: str
    dtype: str  # DuckDB type name
    null_frac: float
    distinct: int
    samples: list = field(default_factory=list)
    min: object = None
    max: object = None
    note: str = ""  # how the load changed or read this column ("converted from text like 16-Mar-23"); UI only

    @property
    def all_null(self) -> bool:
        return self.null_frac >= 1.0

    @property
    def is_numeric(self) -> bool:
        return any(t in self.dtype for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "HUGEINT"))

    @property
    def is_temporal(self) -> bool:
        return any(t in self.dtype for t in ("DATE", "TIME"))


@dataclass
class TableInfo:
    name: str  # DuckDB table name
    source: str  # original filename
    rows: int
    columns: list[ColumnInfo]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def open_connection(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """On-disk DuckDB so large uploads spill to disk instead of RAM.
    Pass None for an in-memory connection (tests)."""
    return duckdb.connect(str(db_path) if db_path else ":memory:")


def table_name_for(filename: str, existing: set[str]) -> str:
    stem = Path(filename).stem.lower()
    name = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_") or "table"
    if name[0].isdigit():
        name = f"t_{name}"
    base, i = name, 2
    while name in existing:
        name = f"{base}_{i}"
        i += 1
    return name


@dataclass
class LoadResult:
    tables: list[TableInfo]
    notices: list[str] = field(default_factory=list)  # non-fatal, shown in the UI


def _q(s: str) -> str:
    """Quote a SQL identifier."""
    return '"' + s.replace('"', '""') + '"'


def _short(e: Exception) -> str:
    """First useful line of a DuckDB/pandas error, not the 30-line dump."""
    lines = [l.strip() for l in str(e).splitlines() if l.strip()]
    skip = ("csv error on line", "original line", "possible solution", "invalid input error: csv error")
    msg = next((l for l in lines if not l.lower().startswith(skip) and not l.startswith(("file =", "delimiter", "quote"))),
               lines[0] if lines else "")
    return f"{type(e).__name__}: {msg[:200]}"


def load_file(con: duckdb.DuckDBPyConnection, path: Path, existing: set[str]) -> LoadResult:
    """Load one file from disk into DuckDB. A CSV is one table; an Excel workbook is one
    table per non-empty sheet. Raises LoadError if nothing usable came out of the file."""
    path = Path(path)
    size_mb = path.stat().st_size / (1024 * 1024)
    if path.stat().st_size == 0:
        raise LoadError(f"{path.name}: file is empty.")
    if size_mb > MAX_FILE_MB:
        raise LoadError(f"{path.name}: {size_mb:.0f} MB exceeds the {MAX_FILE_MB} MB limit.")

    ext = path.suffix.lower()
    existing = set(existing)  # local copy; caller re-derives from returned tables
    result = LoadResult(tables=[])

    def add_table(name: str, source: str, create_sql: str, params: list | None = None):
        if len(existing) >= MAX_FILES:
            raise LoadError(f"At most {MAX_FILES} tables per session (each Excel sheet counts as one).")
        try:
            con.execute(create_sql, params or [])
        except Exception as e:  # duckdb / pandas parse errors
            raise LoadError(f"{source}: could not parse — {_short(e)}") from e
        notes = _promote_text_dates(con, name)
        info = describe_table(con, name, source=source)
        if info.rows == 0:
            con.execute(f"DROP TABLE {_q(name)}")
            return False
        for c in info.columns:  # shown beside the column, not as a banner
            c.note = notes.get(c.name, "")
        existing.add(name)
        result.tables.append(info)
        return True

    if ext in CSV_EXT:
        # DuckDB reads CSV natively: sniffs delimiter, header, types. UTF-8 first; Latin-1 fallback.
        name = table_name_for(path.name, existing)
        base = (f"CREATE TABLE {_q(name)} AS SELECT * FROM read_csv(?, header=true, "
                f"normalize_names=false, null_padding=true, ignore_errors=false")
        try:
            ok = add_table(name, path.name, base + ")", [str(path)])
        except LoadError as e:
            raw = str(e.__cause__).lower()
            if "utf-8" not in raw and "unicode" not in raw:
                raise
            ok = add_table(name, path.name, base + ", encoding='latin-1')", [str(path)])
            result.notices.append(f"{path.name}: not UTF-8, read as Latin-1.")
        if not ok:
            raise LoadError(f"{path.name}: header only, no data rows.")
        ambiguous = _ambiguous_date_notices(con, path, name)
        for c in result.tables[-1].columns:
            if c.name in ambiguous:
                c.note = ambiguous[c.name]

    elif ext in EXCEL_EXT:
        # pandas only for Excel parsing. Every sheet becomes its own table.
        try:
            sheets: dict[str, pd.DataFrame] = pd.read_excel(path, sheet_name=None)
        except Exception as e:
            raise LoadError(f"{path.name}: could not parse — {_short(e)}") from e
        multi = len(sheets) > 1
        for sheet, df in sheets.items():
            source = f"{path.name} [{sheet}]" if multi else path.name
            df.columns = [str(c).strip() for c in df.columns]
            if df.empty or df.shape[1] == 0:
                result.notices.append(f"{source}: empty sheet, skipped.")
                continue
            stem = f"{Path(path.name).stem}_{sheet}" if multi else path.name
            name = table_name_for(stem, existing)
            con.register("_xl_tmp", df)
            try:
                add_table(name, source, f"CREATE TABLE {_q(name)} AS SELECT * FROM _xl_tmp")
            finally:
                con.unregister("_xl_tmp")
        if not result.tables:
            raise LoadError(f"{path.name}: no sheet has any data rows.")

    else:
        raise LoadError(f"{path.name}: unsupported type '{ext}'. Upload CSV or Excel.")

    return result


# Date spellings DuckDB's CSV sniffer doesn't try (it covers ISO and numeric d/m/Y, m/d/Y).
# The Kaggle HR export uses 16-Mar-23 throughout, and a date-as-text column can't be filtered
# by year, bucketed by month, or ranged — every trend question on it refuses.
_TEXT_DATE_FORMATS = [
    ("%d-%b-%y", "day-Mon-yy"), ("%d-%b-%Y", "day-Mon-yyyy"), ("%d/%b/%Y", "day/Mon/yyyy"), ("%d/%b/%y", "day/Mon/yy"),
    ("%d %b %Y", "day Mon yyyy"), ("%d %B %Y", "day Month yyyy"), ("%b %d, %Y", "Mon day, yyyy"), ("%B %d, %Y", "Month day, yyyy"),
    ("%b %d %Y", "Mon day yyyy"), ("%Y-%b-%d", "yyyy-Mon-day"),
]
_LOOKS_DATED = re.compile(r"^\s*(?:\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ,]+\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{4}-[A-Za-z]{3}-\d{1,2})\s*$")
_NUMERIC_DMY = {"%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%d/%m/%y", "%m/%d/%y"}


def _promote_text_dates(con: duckdb.DuckDBPyConnection, name: str) -> dict[str, str]:
    """Retype VARCHAR columns whose every non-null value is a date in one of the spellings
    above. All-or-nothing per column: a column mixing dates and words stays text.
    Returns {column: note}, the note quoting one original value ("converted from text like 16-Mar-23")."""
    notes: dict[str, str] = {}
    for cname, ctype, *_ in con.execute(f"DESCRIBE {_q(name)}").fetchall():
        if ctype != "VARCHAR":
            continue
        sample = [r[0] for r in con.execute(
            f"SELECT {_q(cname)} FROM {_q(name)} WHERE {_q(cname)} IS NOT NULL LIMIT 5").fetchall()]
        if not sample or not all(_LOOKS_DATED.match(str(v)) for v in sample):
            continue
        for fmt, human in _TEXT_DATE_FORMATS:
            bad = con.execute(
                f"SELECT count(*) FROM {_q(name)} WHERE {_q(cname)} IS NOT NULL AND try_strptime(trim({_q(cname)}), ?) IS NULL",
                [fmt]).fetchone()[0]
            if bad == 0:
                lit = "'" + fmt.replace("'", "''") + "'"  # ALTER can't take parameters; fmt is our own constant
                con.execute(f"ALTER TABLE {_q(name)} ALTER COLUMN {_q(cname)} TYPE DATE USING CAST(try_strptime(trim({_q(cname)}), {lit}) AS DATE)")
                notes[cname] = f"converted from text like {str(sample[0]).strip()}"
                break
    return notes


def _ambiguous_date_notices(con: duckdb.DuckDBPyConnection, path: Path, name: str) -> dict[str, str]:
    """DuckDB read a numeric day/month column and picked an order. If every value would also
    read the other way round (no day above 12), say so — the user knows which their file is.
    Returns {column: note}."""
    try:
        row = con.execute("SELECT DateFormat FROM sniff_csv(?)", [str(path)]).fetchone()
    except Exception:
        return {}
    fmt = row[0] if row else None
    if fmt not in _NUMERIC_DMY:
        return {}
    day_first = fmt.startswith("%d")
    out: dict[str, str] = {}
    for cname, ctype, *_ in con.execute(f"DESCRIBE {_q(name)}").fetchall():
        if ctype != "DATE":
            continue
        hi = con.execute(f"SELECT max({'day' if day_first else 'month'}({_q(cname)})) FROM {_q(name)}").fetchone()[0]
        lo = con.execute(f"SELECT max({'month' if day_first else 'day'}({_q(cname)})) FROM {_q(name)}").fetchone()[0]
        if hi is not None and hi <= 12 and lo is not None and lo <= 12:
            this, other = ("day/month/year", "month/day/year") if day_first else ("month/day/year", "day/month/year")
            out[cname] = f"read as {this}; every value would also read as {other} — if the file is {other}, these dates are wrong"
    return out


def describe_table(con: duckdb.DuckDBPyConnection, name: str, source: str = "") -> TableInfo:
    """Schema + lightweight stats. This is what the model sees; rows are never sent."""
    q = _q
    rows = con.execute(f"SELECT count(*) FROM {q(name)}").fetchone()[0]
    cols = con.execute(f"DESCRIBE {q(name)}").fetchall()  # (name, type, null, key, default, extra)
    columns: list[ColumnInfo] = []
    for cname, ctype, *_ in cols:
        stats = con.execute(
            f"SELECT count(*) - count({q(cname)}), count(DISTINCT {q(cname)}) FROM {q(name)}"
        ).fetchone()
        nulls, distinct = stats
        samples = [
            r[0]
            for r in con.execute(
                f"SELECT DISTINCT {q(cname)} FROM {q(name)} WHERE {q(cname)} IS NOT NULL LIMIT {SAMPLE_VALUES}"
            ).fetchall()
        ]
        col = ColumnInfo(
            name=cname,
            dtype=ctype,
            null_frac=(nulls / rows) if rows else 1.0,
            distinct=distinct,
            samples=samples,
        )
        if rows and (col.is_numeric or col.is_temporal):
            col.min, col.max = con.execute(
                f"SELECT min({q(cname)}), max({q(cname)}) FROM {q(name)}"
            ).fetchone()
        columns.append(col)
    return TableInfo(name=name, source=source or name, rows=rows, columns=columns)


def schema_text(tables: list[TableInfo]) -> str:
    """Compact schema rendering for prompts and the UI. No data rows."""
    out = []
    for t in tables:
        out.append(f"TABLE {t.name}  ({t.rows:,} rows, from {t.source})")
        for c in t.columns:
            flags = []
            if c.all_null:
                flags.append("ALL NULL")
            elif c.null_frac > 0:
                flags.append(f"{c.null_frac:.0%} null")
            if c.min is not None:
                flags.append(f"range {c.min}..{c.max}")
            fmt = lambda v: repr(v) if isinstance(v, str) else str(v)  # noqa: E731
            if c.distinct <= SAMPLE_VALUES and len(c.samples) == c.distinct:
                sample = f"values: {', '.join(fmt(s) for s in sorted(c.samples, key=str))}"
            else:
                sample = f"e.g. {', '.join(fmt(s) for s in c.samples[:3])}"
            meta = f"  [{'; '.join(flags)}]" if flags else ""
            out.append(f"  - {c.name}: {c.dtype}  distinct={c.distinct}  {sample}{meta}")
    return "\n".join(out)

#!/usr/bin/env python
"""Plan or audit an index cleanup for an enterprise geodatabase in SQL Server.

An index cleanup list is built from sys.dm_db_index_usage_stats: drop what
nobody reads, and drop the second copy of anything indexed twice. Three things
go wrong when the database is a geodatabase. The usage counters restart with
the database engine, so a list taken after a patch reboot calls every index
unused. A list that marks one copy of an identical pair "unused" and the other
"duplicate" drops both. And a versioned table keeps its edits in an adds table,
so an attribute index on the base table has a twin there, and the two are only
ever used together.

This tool reads an exported index list and the geodatabase table registry, and
applies those rules. Without --drops it plans its own cleanup. With --drops it
audits somebody else's DROP INDEX list, for example the output of
sp_IndexCleanup, and refuses every drop the rules forbid.

    python sdeindex.py --self-test
    python sdeindex.py --print-query
    python sdeindex.py indexes.csv --registry registry.csv
    python sdeindex.py indexes.csv --registry registry.csv --drops cleanup.sql
    python sdeindex.py indexes.csv --registry registry.csv --out drop.sql --apply

Nothing is written without --apply, and nothing is ever run against a server.
--apply writes a DROP INDEX script for a human to read and run.

Exit codes: 0 no listed drop was refused, 1 the audited list holds a drop the
rules refuse, 2 a file could not be read or written, 3 the input was refused,
64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags, except the uptime floor.
# =============================================================================

# Days of usage counters an "unused" verdict needs. Microsoft: the counters
# are initialized to empty whenever the database engine starts. 14 is the
# uptime under which sp_IndexCleanup warns that usage may not be
# representative. It is borrowed, not measured: a quarterly report reads its
# index four times a year, and no floor short of a quarter sees it.
DEFAULT_MIN_UPTIME_DAYS = 14

# Columns the index export must have. The query --print-query shows writes
# exactly these names.
INDEX_COLUMNS = (
    "schema_name", "table_name", "index_name", "type_desc", "is_unique",
    "is_primary_key", "is_unique_constraint", "key_columns",
    "included_columns", "filter_definition", "user_seeks", "user_scans",
    "user_lookups", "user_updates")
UPTIME_COLUMN = "uptime_hours"
REGISTRY_COLUMNS = ("registration_id", "owner", "table_name")

# The only index type this tool judges. Everything else is kept.
JUDGED_TYPE = "NONCLUSTERED"

# Microsoft: sys.dm_db_index_usage_stats returns no information about spatial
# or memory-optimized indexes, so they always look unread.
UNSEEN_TYPES = ("SPATIAL", "NONCLUSTERED HASH")

# Columns Esri documents on the delta tables of a traditionally versioned
# table. An index over one of them is the geodatabase's, not the user's.
DELTA_COLUMNS = frozenset(("sde_state_id", "sde_deletes_row_id", "deleted_at"))

# Esri: the ObjectID index in an enterprise geodatabase is named
# r_#_sde_rowid_uk, in practice r<registration_id>_sde_rowid_uk.
ROWID_INDEX = re.compile(r"^r_?\d+_sde_rowid_uk$", re.I)

# Esri names the delta tables a<registration_id> and d<registration_id> in
# older help, and a_<registration_id> in current help. Both are read.
DELTA_TABLE = re.compile(r"^([ad])_?(\d+)$", re.I)

# Esri's geodatabase system tables in SQL Server all start SDE_ or GDB_.
SYSTEM_TABLE = re.compile(r"^(sde|gdb)_", re.I)

DROP, REVIEW, HOLD, KEEP = "DROP", "REVIEW", "HOLD", "KEEP"
SAFE, REFUSED, UNPROVEN = "SAFE", "REFUSED", "UNPROVEN"

INDEX_QUERY = """\
-- sdeindex index export. SQL Server 2017 or later (STRING_AGG).
-- Run it in the geodatabase, then save the result as CSV with a header row.
SELECT s.name AS schema_name,
       t.name AS table_name,
       i.name AS index_name,
       i.type_desc,
       CAST(i.is_unique AS int) AS is_unique,
       CAST(i.is_primary_key AS int) AS is_primary_key,
       CAST(i.is_unique_constraint AS int) AS is_unique_constraint,
       COALESCE((SELECT STRING_AGG(CAST(QUOTENAME(c.name) + CASE
                    WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE ' ASC'
                    END AS nvarchar(max)), '+')
                    WITHIN GROUP (ORDER BY ic.key_ordinal)
                 FROM sys.index_columns AS ic
                 JOIN sys.columns AS c ON c.object_id = ic.object_id
                                      AND c.column_id = ic.column_id
                 WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                   AND ic.key_ordinal > 0), '') AS key_columns,
       COALESCE((SELECT STRING_AGG(CAST(QUOTENAME(c.name) AS nvarchar(max)),
                                   '+')
                    WITHIN GROUP (ORDER BY c.name)
                 FROM sys.index_columns AS ic
                 JOIN sys.columns AS c ON c.object_id = ic.object_id
                                      AND c.column_id = ic.column_id
                 WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                   AND ic.is_included_column = 1), '') AS included_columns,
       COALESCE(i.filter_definition, '') AS filter_definition,
       COALESCE(u.user_seeks, 0) AS user_seeks,
       COALESCE(u.user_scans, 0) AS user_scans,
       COALESCE(u.user_lookups, 0) AS user_lookups,
       COALESCE(u.user_updates, 0) AS user_updates,
       up.uptime_hours
FROM sys.indexes AS i
JOIN sys.tables AS t ON t.object_id = i.object_id
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
CROSS JOIN (SELECT DATEDIFF(hour, sqlserver_start_time, SYSDATETIME())
                   AS uptime_hours FROM sys.dm_os_sys_info) AS up
LEFT JOIN sys.dm_db_index_usage_stats AS u
       ON u.database_id = DB_ID() AND u.object_id = i.object_id
      AND u.index_id = i.index_id
WHERE i.type > 0 AND i.is_hypothetical = 0 AND t.is_ms_shipped = 0
ORDER BY s.name, t.name, i.name;
"""

REGISTRY_QUERY = """\
-- sdeindex registry export. Use dbo.SDE_table_registry in a geodatabase
-- whose system tables are in the dbo schema.
SELECT registration_id, owner, table_name
FROM sde.SDE_table_registry
ORDER BY registration_id;
"""


class Refused(Exception):
    """The input cannot be judged safely, so nothing is judged."""


# ------------------------------------------------------------------- reading

_COLUMN = re.compile(r"\s*(?:\[((?:[^\]]|\]\])+)\]|([^\s\[\]+]+))"
                     r"(?:\s+(ASC|DESC))?\s*(?:\+|$)", re.I)


def split_columns(text):
    """'[A] ASC+[B] DESC' as (('a', 'ASC'), ('b', 'DESC')). Names fold case."""
    text = (text or "").strip()
    items = []
    pos = 0
    while pos < len(text):
        m = _COLUMN.match(text, pos)
        if not m:
            raise Refused("cannot read the column list %r. Columns are "
                          "joined with '+', as the export query writes them."
                          % text)
        name = m.group(2) if m.group(1) is None else \
            m.group(1).replace("]]", "]")
        items.append((name.lower(), (m.group(3) or "ASC").upper()))
        pos = m.end()
    return tuple(items)


def parse_flag(text, column):
    value = (text or "").strip().lower()
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise Refused("%s is %r, not 1 or 0" % (column, text))


def parse_count(text, column):
    """A usage counter. Blank or NULL is 0: Microsoft adds the row on first use."""
    value = (text or "").strip()
    if value == "" or value.upper() == "NULL":
        return 0
    if not value.isdigit():
        raise Refused("%s is %r, not a whole number" % (column, text))
    return int(value)


def bracket(name):
    return "[%s]" % name.replace("]", "]]")


class Index(object):
    """One row of the index export."""

    def __init__(self, schema, table, name, kind=JUDGED_TYPE, unique=False,
                 pk=False, uc=False, keys=(), includes=(), filter_text="",
                 reads=0, writes=0, line=0):
        self.schema, self.table, self.name = schema, table, name
        self.kind = kind.strip().upper()
        self.unique, self.pk, self.uc = unique, pk, uc
        self.keys = tuple(keys)
        self.includes = frozenset(includes)
        self.filter = " ".join(filter_text.split())
        self.reads, self.writes, self.line = reads, writes, line
        self.key = (schema.lower(), table.lower(), name.lower())
        self.reason = None

    @property
    def label(self):
        return "%s.%s.%s" % (bracket(self.schema), bracket(self.table),
                             bracket(self.name))

    @property
    def signature(self):
        """Two indexes with one signature on one table are the same index."""
        return (self.kind, self.keys, tuple(sorted(self.includes)),
                self.filter)

    @property
    def columns(self):
        return set(k for k, _ in self.keys) | set(self.includes)


def index_from_row(row, line):
    """One export row as an Index. Raises Refused on a value it cannot read."""
    try:
        if None in row or None in row.values():
            raise Refused("the row has %s fields than the header. A value "
                          "with an unquoted comma splits a row in two."
                          % ("more" if None in row else "fewer"))
        return Index(
            row["schema_name"].strip(), row["table_name"].strip(),
            row["index_name"].strip(), row["type_desc"],
            parse_flag(row["is_unique"], "is_unique"),
            parse_flag(row["is_primary_key"], "is_primary_key"),
            parse_flag(row["is_unique_constraint"], "is_unique_constraint"),
            split_columns(row["key_columns"]),
            [name for name, _ in split_columns(row["included_columns"])],
            row["filter_definition"],
            sum(parse_count(row[c], c)
                for c in ("user_seeks", "user_scans", "user_lookups")),
            parse_count(row["user_updates"], "user_updates"), line)
    except Refused as exc:
        raise Refused("line %d: %s" % (line, exc))


def uptime_hours(rows):
    """The one uptime the export recorded, None when it recorded none."""
    values = set((r.get(UPTIME_COLUMN) or "").strip() for r in rows)
    if values <= set([""]):
        return None
    if "" in values or len(values) > 1:
        raise Refused("the uptime_hours column holds %s. One export runs one "
                      "query, so it has one uptime; this file joins two."
                      % ", ".join(repr(v) for v in sorted(values)))
    value = values.pop()
    if not value.isdigit():
        raise Refused("uptime_hours is %r, not a whole number of hours"
                      % value)
    return int(value)


def registry_from_rows(rows):
    """{registration_id: (owner, table_name)}, folded to lower case."""
    registry = {}
    for line, row in enumerate(rows, 2):
        rid = (row["registration_id"] or "").strip()
        if not rid.isdigit():
            raise Refused("registry line %d: registration_id is %r"
                          % (line, rid))
        if int(rid) in registry:
            raise Refused("registry line %d: registration_id %s is listed "
                          "twice" % (line, rid))
        registry[int(rid)] = ((row["owner"] or "").strip().lower(),
                              (row["table_name"] or "").strip().lower())
    return registry


# ------------------------------------------------------------- the decisions

def protection(ix, delta_reason=None):
    """Why this index is never dropped, or None when the rules may judge it."""
    if ix.key[0] == "sde":
        return "is in the sde schema, which holds the geodatabase system tables"
    if SYSTEM_TABLE.match(ix.table):
        return "is on %s, a geodatabase system table" % ix.table
    if ROWID_INDEX.match(ix.name):
        return "is the geodatabase ObjectID index, which Esri says cannot " \
               "be removed"
    if delta_reason:
        return delta_reason
    if ix.pk:
        return "backs a PRIMARY KEY constraint"
    if ix.uc:
        return "backs a UNIQUE constraint"
    if ix.kind in UNSEEN_TYPES:
        return "is a %s index, which the usage counters never see" % ix.kind
    if ix.kind != JUDGED_TYPE:
        return "is a %s index, which this tool does not judge" % ix.kind
    if ix.unique:
        return "is unique, so it enforces a rule as well as serving reads"
    return None


def classify(ix, registry):
    """(home table, protection reason). An adds-table index lives at its base."""
    home = ix.key[:2]
    m = DELTA_TABLE.match(ix.table)
    if not m:
        return home, protection(ix)
    if registry is None:
        raise Refused("%s is on %s, which is named like a geodatabase delta "
                      "table, and no --registry was given. Without the "
                      "registry the twin of every versioned index is unknown."
                      % (ix.label, ix.table))
    entry = registry.get(int(m.group(2)))
    if entry is None or entry[0] != ix.key[0]:
        return home, protection(
            ix, "is on %s, which is named like a delta table, but no "
                "registry row in schema %s owns id %s" % (ix.table, ix.schema,
                                                         m.group(2)))
    if m.group(1).lower() == "d":
        return home, protection(
            ix, "is on %s, the deletes table of %s, which the geodatabase "
                "maintains" % (ix.table, entry[1]))
    if ix.columns & DELTA_COLUMNS:
        return home, protection(
            ix, "is on %s, the adds table of %s, and indexes a column the "
                "geodatabase maintains" % (ix.table, entry[1]))
    return entry, protection(ix)


class Unit(object):
    """An index, or a base-table index and its adds-table twin."""

    def __init__(self, members):
        self.members = members
        self.reads = sum(m.reads for m in members)
        self.writes = sum(m.writes for m in members)
        self.group = None

    @property
    def protected(self):
        return any(m.reason for m in self.members)

    @property
    def label(self):
        return " + twin ".join(m.label for m in self.members)

    def sort_key(self):
        """The unit an identical set keeps: protected, then paired, then read."""
        return (0 if self.protected else 1, -len(self.members), -self.reads,
                self.members[0].key)


class Model(object):
    def __init__(self, indexes, groups):
        self.indexes = indexes
        self.groups = groups
        self.units = [u for g in groups for u in g]
        self.unit_of = dict((m.key, u) for u in self.units for m in u.members)

    def find(self, schema, table, name):
        return [ix for ix in self.indexes
                if ix.key[1:] == (table.lower(), name.lower())
                and (schema is None or ix.key[0] == schema.lower())]


def build(indexes, registry=None):
    """Group identical indexes and pair each adds-table index with its twin."""
    seen = {}
    for ix in indexes:
        if ix.key in seen:
            raise Refused("%s is on line %d and line %d. Two exports were "
                          "joined, so its usage would be counted twice."
                          % (ix.label, seen[ix.key].line, ix.line))
        seen[ix.key] = ix
    buckets = {}
    for ix in indexes:
        home, ix.reason = classify(ix, registry)
        buckets.setdefault((home, ix.signature), []).append(ix)
    groups = []
    for (home, _), members in buckets.items():
        base = sorted((m for m in members if m.key[:2] == home),
                      key=lambda m: m.key)
        adds = sorted((m for m in members if m.key[:2] != home),
                      key=lambda m: m.key)
        # Identical twins pair in name order. Which copy pairs with which
        # does not matter: every unit in a group is the same index.
        units = [Unit([b, a]) for b, a in zip(base, adds)]
        units += [Unit([m]) for m in base[len(adds):] + adds[len(base):]]
        for u in units:
            u.group = units
        groups.append(sorted(units, key=Unit.sort_key))
    groups.sort(key=lambda g: g[0].members[0].key)
    return Model(indexes, groups)


def days(hours):
    return "%.1f days" % (hours / 24.0)


def usage_verdict(unit, uptime, min_days):
    """(ok, reason) for an unread unit, judged against the uptime."""
    if uptime is None:
        return False, "no reads, but the export carries no uptime_hours, so " \
                      "the counters may have restarted an hour ago"
    if uptime < min_days * 24:
        return False, "no reads, but the counters cover only %s of uptime " \
                      "and the floor is %d days" % (days(uptime), min_days)
    return True, "no reads in %s of uptime, %d write(s) paid for nothing" \
                 % (days(uptime), unit.writes)


def plan(model, uptime, min_days=DEFAULT_MIN_UPTIME_DAYS):
    """[(action, unit, reason)] for every unit."""
    out = []
    for group in model.groups:
        keeper = group[0]
        reads = sum(u.reads for u in group)
        if keeper.protected:
            out.append((KEEP, keeper, "protected"))
        elif reads:
            out.append((KEEP, keeper, "read %d time(s)" % reads))
        else:
            ok, why = usage_verdict(keeper, uptime, min_days)
            if not ok:
                out.append((HOLD, keeper, why))
            elif len(group) > 1:
                out.append((REVIEW, keeper,
                            "every copy is unread, and this is the one the "
                            "plan keeps. Judge it in a later run, after its "
                            "duplicates are gone"))
            else:
                out.append((DROP, keeper, why))
        for u in group[1:]:
            if u.protected:
                out.append((KEEP, u, "protected"))
            else:
                out.append((DROP, u, "identical to %s, which stays"
                            % keeper.members[0].label))
    return out


def audit(model, entries, uptime, min_days=DEFAULT_MIN_UPTIME_DAYS):
    """[(verdict, label, reason)] for each listed (schema, table, index, line)."""
    found = {}
    listed = set()
    for entry in entries:
        schema, table, name, line = entry
        hits = model.find(schema, table, name)
        if len(hits) == 1:
            found[entry] = hits[0]
            listed.add(hits[0].key)
    verdict = {}
    candidates = []
    for unit in model.units:
        mine = [m for m in unit.members if m.key in listed]
        if not mine:
            continue
        if unit.protected:
            for m in mine:
                other = [o for o in unit.members if o.reason][0]
                verdict[m.key] = (REFUSED, m.reason or "its twin %s %s" % (
                    other.label, other.reason))
        elif len(mine) < len(unit.members):
            for m in mine:
                twin = [o for o in unit.members if o is not m][0]
                verdict[m.key] = (REFUSED, "leaves its twin %s behind. A "
                                  "versioned query reads both, so they go "
                                  "together or not at all" % twin.label)
        else:
            candidates.append(unit)
    for group in model.groups:
        cands = [u for u in group if u in candidates]
        if not cands:
            continue
        stays = [u for u in group if u not in cands]
        if not stays:
            stays = [cands.pop(0)]
            if len(group) > 1:
                why = (REFUSED, "the list drops every copy of an identical "
                       "set. Keep this one; drop the others first")
            elif stays[0].reads:
                why = (UNPROVEN, "read %d time(s), and not a copy of an index "
                       "that stays. These rules cannot prove it redundant"
                       % stays[0].reads)
            else:
                ok, text = usage_verdict(stays[0], uptime, min_days)
                why = (SAFE if ok else REFUSED, text)
            for m in stays[0].members:
                verdict[m.key] = why
        for u in cands:
            for m in u.members:
                verdict[m.key] = (SAFE, "identical to %s, which stays"
                                  % stays[0].members[0].label)
    out = []
    for entry in entries:
        schema, table, name, line = entry
        if entry in found:
            ix = found[entry]
            out.append(verdict[ix.key] + (ix.label,))
            continue
        hits = model.find(schema, table, name)
        where = "%s.%s" % (schema + "." if schema else "", table)
        out.append((REFUSED, "line %d: %s is %s" % (
            line, name, "in more than one schema; name the schema"
            if hits else "not in the index export, so nothing shows it is "
                         "safe"), "%s.%s" % (where, name)))
    return [(v, label, reason) for v, reason, label in out]


# --------------------------------------------------------- the DROP list

_NAME = r'(?:\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")+"|[A-Za-z_@#][\w@#$]*)'
_DROP = re.compile(r"DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?(%s)\s+ON\s+(%s"
                   r"(?:\s*\.\s*%s){0,2})" % (_NAME, _NAME, _NAME), re.I)
_DROP_WORDS = re.compile(r"\bDROP\s+INDEX\b", re.I)
_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)


def unquote(name):
    if name[0] == "[":
        return name[1:-1].replace("]]", "]")
    if name[0] == '"':
        return name[1:-1].replace('""', '"')
    return name


def parse_drops(text):
    """[(schema or None, table, index, line)] from DROP INDEX statements."""
    # Comments become blanks of the same shape, so line numbers survive and a
    # commented-out DROP is never audited as if it would run.
    clean = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    out = []
    for word in _DROP_WORDS.finditer(clean):
        line = clean.count("\n", 0, word.start()) + 1
        m = _DROP.match(clean, word.start())
        if not m:
            raise Refused("line %d: a DROP INDEX this tool cannot read. Write "
                          "DROP INDEX name ON schema.table, one per index."
                          % line)
        if clean[m.end():].lstrip().startswith(","):
            raise Refused("line %d: one DROP INDEX names several indexes. "
                          "Write one statement per index." % line)
        parts = [unquote(p) for p in re.findall(_NAME, m.group(2))]
        schema = parts[-2] if len(parts) > 1 else None
        out.append((schema, parts[-1], unquote(m.group(1)), line))
    if not out:
        raise Refused("the list holds no DROP INDEX statement. Is it the "
                      "right file?")
    return out


def drop_script(units, source):
    """A DROP INDEX script, one comment per unit. Never run by this tool."""
    lines = ["-- sdeindex drop script: %d index(es) in %d unit(s), from %s."
             % (sum(len(u.members) for u, _ in units), len(units), source),
             "-- Read it, then run it yourself. sdeindex runs nothing."]
    for unit, reason in units:
        lines.append("")
        lines.append("-- %s" % reason)
        if len(unit.members) > 1:
            lines.append("-- twin pair: base table and adds table, dropped "
                         "together")
        for m in unit.members:
            lines.append("DROP INDEX %s ON %s.%s;" % (
                bracket(m.name), bracket(m.schema), bracket(m.table)))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ output

def describe_uptime(uptime, min_days):
    if uptime is None:
        return "uptime: not in the export, so no index is called unused"
    return "uptime: %s of usage counters, floor %d days%s" % (
        days(uptime), min_days, "" if uptime >= min_days * 24 else
        ", so no index is called unused")


def describe_plan(decisions):
    lines = []
    for action in (DROP, REVIEW, HOLD):
        for act, unit, reason in decisions:
            if act == action:
                lines.append("%-9s %s  %s" % (act, unit.label, reason))
    kept = [u for a, u, _ in decisions if a == KEEP]
    protected = sum(1 for u in kept if u.protected)
    lines.append("KEEP      %d unit(s): %d protected by a rule, %d read"
                 % (len(kept), protected, len(kept) - protected))
    count = dict((a, sum(1 for d in decisions if d[0] == a))
                 for a in (DROP, REVIEW, HOLD))
    lines.append("")
    lines.append("VERDICT: %d drop(s) planned, %d for review, %d held for "
                 "uptime" % (count[DROP], count[REVIEW], count[HOLD]))
    return lines


def describe_audit(results):
    lines = ["%-9s %s  %s" % r for r in results]
    refused = sum(1 for r in results if r[0] == REFUSED)
    lines.append("")
    if refused:
        lines.append("VERDICT: %d of %d listed drop(s) refused. Do not run "
                     "this list as it stands." % (refused, len(results)))
    else:
        lines.append("VERDICT: no listed drop is refused (%d safe, %d "
                     "unproven)" % (
                         sum(1 for r in results if r[0] == SAFE),
                         sum(1 for r in results if r[0] == UNPROVEN)))
    return lines


def audit_units(model, results):
    """The SAFE units of an audit, in plan order, for the script."""
    safe = set(label for v, label, _ in results if v == SAFE)
    out = []
    for unit in model.units:
        if any(m.label in safe for m in unit.members):
            reason = [r for v, label, r in results
                      if label == unit.members[0].label][0]
            out.append((unit, reason))
    return out

#!/usr/bin/env python
"""Plan or audit an index cleanup for an enterprise geodatabase in SQL Server.

An index cleanup list is built from sys.dm_db_index_usage_stats: drop what
nobody reads, and drop the second copy of anything indexed twice. Three things
go wrong when the database is a geodatabase. The usage counters restart with
the database engine, so a list taken after a patch reboot calls every index
unused. A list that marks one copy of an identical pair "unused" and the other
"duplicate" drops both. And a versioned table keeps its edits in an adds table
that can carry a copy of an attribute index, so a rule that judges the two
copies one at a time can drop half of a pair. This tool treats an identical
copy on the adds table as the twin and moves the pair together.

This tool reads an exported index list and the geodatabase table registry, and
applies those rules. Without --drops it plans its own cleanup. With --drops it
audits somebody else's script of DROP INDEX or ALTER INDEX ... DISABLE
statements, the second being the form sp_IndexCleanup writes, and refuses every
removal the rules forbid.

    python sdeindex.py --self-test
    python sdeindex.py --print-query
    python sdeindex.py indexes.csv --registry registry.csv
    python sdeindex.py indexes.csv --registry registry.csv --drops cleanup.sql
    python sdeindex.py indexes.csv --min-uptime-days 30
    python sdeindex.py indexes.csv --registry registry.csv --out drop.sql --apply

Nothing is written without --apply, and nothing is ever run against a server.
--apply writes a DROP INDEX script for a human to read and run. It refuses to
overwrite a file that exists.

Exit codes: 0 every listed drop is safe or no drop is listed, 1 the audited list
holds a drop the rules refuse or cannot prove, 2 a file could not be read or
written, 3 the input was refused, 64 usage error.
"""

from __future__ import print_function

import argparse
import codecs
import contextlib
import csv
import importlib.util
import io
import os
import re
import shutil
import subprocess
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

# Columns of the delta tables of a traditionally versioned table. Esri's Oracle
# help documents D<n>_IDX1 over SDE_STATE_ID and SDE_DELETES_ROW_ID, D<n>_PK
# over DELETED_AT, SDE_DELETES_ROW_ID and SDE_STATE_ID, and an adds-table index
# over SDE_STATE_ID. Its SQL Server help names no index. This tool assumes the
# same column names there, and protects any index over one of them.
DELTA_COLUMNS = frozenset(("sde_state_id", "sde_deletes_row_id", "deleted_at"))

# Esri's SDK help says an attribute index on the object id cannot be deleted.
# It does not document the name. This pattern is a guess at the name; a wrong
# guess costs nothing, because the ObjectID index is unique and the unique rule
# below protects it anyway.
ROWID_INDEX = re.compile(r"^r_?[0-9]+_sde_rowid_uk$", re.I | re.A)

# Esri's SQL Server help names the delta tables a_<registration_id> and
# d_<registration_id>, in the schema of the versioned table. Its Oracle help
# writes A<n> and D<n>. Both spellings are read.
DELTA_TABLE = re.compile(r"^([ad])_?([0-9]+)$", re.I | re.A)

# Esri's SQL Server help lists geodatabase system tables that start SDE_ and
# others that start GDB_.
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
WHERE i.type > 0 AND i.is_hypothetical = 0 AND i.is_disabled = 0
  AND t.is_ms_shipped = 0
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


def whole(text):
    """True for a run of 1 to 19 ASCII digits. str.isdigit also accepts '2'
    with a superscript, which int() then rejects with a crash instead of a
    refusal. Python 3.11 and later also refuse int() on 4300 digits or more.
    19 digits is the width of the largest bigint, the widest counter."""
    return re.match(r"[0-9]{1,19}\Z", text) is not None


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
    if not whole(value):
        raise Refused("%s is %r, not a whole number of 19 digits or fewer"
                      % (column, text))
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
    if not whole(value):
        raise Refused("uptime_hours is %r, not a whole number of hours"
                      % value)
    return int(value)


def registry_from_rows(pairs):
    """{registration_id: (owner, table_name)} from (line, row) pairs, folded
    to lower case."""
    registry = {}
    for line, row in pairs:
        rid = (row["registration_id"] or "").strip()
        if not whole(rid):
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
    # Digits that int() cannot take (4300 or more on Python 3.11) name no row.
    entry = registry.get(int(m.group(2))) if whole(m.group(2)) else None
    if entry is not None and entry[1] == ix.key[1] and entry[0] == ix.key[0]:
        # A table that is itself registered under the name a5 is not the
        # adds table of anything.
        return home, protection(ix)
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
        # An audit looks up every listed drop, so index the export once.
        self.by_name = {}
        for ix in indexes:
            self.by_name.setdefault(ix.key[1:], []).append(ix)

    def find(self, schema, table, name):
        return [ix for ix in self.by_name.get((table.lower(), name.lower()), [])
                if schema is None or ix.key[0] == schema.lower()]


def build(indexes, registry=None):
    """Group identical indexes and pair each adds-table index with its twin."""
    seen = {}
    for ix in indexes:
        if ix.key in seen:
            raise Refused("%s is on line %d and line %d. Either two exports "
                          "were joined, or two names differ only in case, "
                          "which this tool folds together. Its usage would "
                          "be counted twice." % (ix.label, seen[ix.key].line,
                                                 ix.line))
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
                verdict[m.key] = (REFUSED, "leaves its twin %s behind. The "
                                  "two serve one versioned table, so they "
                                  "go together or not at all" % twin.label)
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
        label = ".".join(bracket(p) for p in (schema, table, name) if p)
        out.append((REFUSED, "line %d: %s is %s" % (
            line, bracket(name), "in more than one schema; name the schema"
            if hits else "not in the index export, so nothing shows it is "
                         "safe"), label))
    return [(v, label, reason) for v, reason, label in out]


# --------------------------------------------------------- the DROP list

_NAME = r'(?:\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")+"|[A-Za-z_@#][\w@#$]*)'
_TABLE = r"%s(?:\s*\.\s*%s){0,2}" % (_NAME, _NAME)
_TARGET = r"(?P<name>%s)\s+ON\s+(?P<table>%s)" % (_NAME, _TABLE)
_DROP = re.compile(r"DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?" + _TARGET, re.I)
_ALTER = re.compile(r"ALTER\s+INDEX\s+" + _TARGET + r"\s+DISABLE\b", re.I)
_CONSTRAINT = re.compile(
    r"ALTER\s+TABLE\s+(?P<table>%s)\s+DROP\s+CONSTRAINT\s+"
    r"(?:IF\s+EXISTS\s+)?(?P<name>%s)" % (_TABLE, _NAME), re.I)
_WORDS = re.compile(
    r"\b(DROP\s+INDEX|ALTER\s+INDEX|ALTER\s+TABLE|DROP\s+TABLE|"
    r"CREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?(?:COLUMNSTORE\s+)?"
    r"INDEX)\b", re.I)
_REWRITE = re.compile(r"\bDROP_EXISTING\s*=\s*ON\b", re.I)
_DISABLE = re.compile(r"\bDISABLE\b", re.I)
_DROP_CONSTRAINT = re.compile(r"\bDROP\s+CONSTRAINT\b", re.I)
_WITH = re.compile(r"\s*WITH\s*\(", re.I)

# One left-to-right scan, so a '--' or '/*' inside a string or a bracketed
# name is not a comment, and a quote inside a comment is not a string. A lone
# quote or bracket at the end of the alternatives never closes.
_SCAN = re.compile(r"--[^\r\n]*|/\*|'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]"
                   r"|\"(?:[^\"]|\"\")*\"|['\[\"]")
_BLOCK = re.compile(r"/\*|\*/")


def unquote(name):
    if name[0] == "[":
        return name[1:-1].replace("]]", "]")
    if name[0] == '"':
        return name[1:-1].replace('""', '"')
    return name


def blank(text):
    """The text with every character but a line break turned into a space."""
    return re.sub(r"[^\r\n]", " ", text)


def strip_comments(text):
    """The script with each comment blanked to spaces of the same shape, so line
    numbers survive. Block comments nest, as T-SQL nests them. Raises Refused
    for a string, quoted name or block comment that never closes."""
    out = []
    pos = 0
    while True:
        m = _SCAN.search(text, pos)
        if not m:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:m.start()])
        token, end = m.group(), m.end()
        if token == "/*":
            depth = 1
            while depth:
                k = _BLOCK.search(text, end)
                if not k:
                    break
                depth += 1 if k.group() == "/*" else -1
                end = k.end()
            if depth:
                token = None
            else:
                token = blank(text[m.start():end])
        elif token.startswith("--"):
            token = blank(token)
        elif len(token) == 1:
            token = None
        if token is None:
            raise Refused("line %d: a comment, string or quoted name that "
                          "never closes. Nothing after it can be read."
                          % (text.count("\n", 0, m.start()) + 1))
        out.append(token)
        pos = end


def more_targets(clean, pos):
    """True when the statement that ends at pos goes on to name another target:
    a comma, after an optional WITH (...) clause. An unbalanced clause counts."""
    m = _WITH.match(clean, pos)
    if m:
        depth, pos = 1, m.end()
        while depth and pos < len(clean):
            depth += (clean[pos] == "(") - (clean[pos] == ")")
            pos += 1
        if depth:
            return True
    return clean[pos:].lstrip().startswith(",")


def parse_drops(text):
    """[(schema or None, table, index, line)] from the statements that take an
    index out of use: DROP INDEX, ALTER INDEX ... DISABLE, which is what
    sp_IndexCleanup scripts, and ALTER TABLE ... DROP CONSTRAINT, which it
    scripts for a unique constraint. Other ALTER INDEX statements are not
    removals. DROP TABLE removes every index on the table, so it is refused."""
    clean = strip_comments(text)
    words = list(_WORDS.finditer(clean))
    out = []
    for n, word in enumerate(words):
        line = clean.count("\n", 0, word.start()) + 1
        kind = " ".join(word.group(1).upper().split())
        if kind == "DROP TABLE":
            raise Refused("line %d: DROP TABLE removes every index on the "
                          "table, and this tool does not judge it. Take it "
                          "out of the list." % line)
        if kind == "DROP INDEX":
            m = _DROP.match(clean, word.start())
            shape = "DROP INDEX name ON schema.table"
        else:
            # The statement ends at a semicolon or at the next statement
            # this tool knows.
            stops = [len(clean)] + [i for i in (
                clean.find(";", word.start()),
                words[n + 1].start() if n + 1 < len(words) else -1) if i >= 0]
            if kind.startswith("CREATE"):
                # A rewrite of an index changes the copy a drop says "stays".
                if _REWRITE.search(clean, word.start(), min(stops)):
                    raise Refused(
                        "line %d: CREATE INDEX ... DROP_EXISTING = ON "
                        "rewrites an index, so a copy that a drop leaves "
                        "behind may not be the index judged. Audit the DROP "
                        "statements in a file of their own." % line)
                continue
            marker = _DISABLE if kind == "ALTER INDEX" else _DROP_CONSTRAINT
            if not marker.search(clean, word.start(), min(stops)):
                continue
            if kind == "ALTER INDEX":
                m = _ALTER.match(clean, word.start())
                shape = "ALTER INDEX name ON schema.table DISABLE"
            else:
                m = _CONSTRAINT.match(clean, word.start())
                shape = "ALTER TABLE schema.table DROP CONSTRAINT name"
        if not m or m.group("name").upper() == "ALL":
            raise Refused("line %d: a statement this tool cannot read. Write "
                          "%s, one per index." % (line, shape))
        if kind != "ALTER INDEX" and more_targets(clean, m.end()):
            raise Refused("line %d: one statement names several indexes, or "
                          "its WITH clause never closes. Write one statement "
                          "per index." % line)
        parts = [unquote(p) for p in re.findall(_NAME, m.group("table"))]
        schema = parts[-2] if len(parts) > 1 else None
        out.append((schema, parts[-1], unquote(m.group("name")), line))
    if not out:
        raise Refused("the list holds no DROP INDEX, ALTER INDEX ... "
                      "DISABLE or DROP CONSTRAINT statement. Is it the right "
                      "file?")
    return out


def one_line(text):
    """The text with line breaks written out, so a name that holds one cannot
    end a '--' comment and leave the rest of the name as live SQL."""
    return text.replace("\r", "\\r").replace("\n", "\\n")


def drop_script(units, source):
    """A DROP INDEX script, one comment per unit. Never run by this tool."""
    lines = ["-- sdeindex drop script: %d index(es) in %d unit(s), from %s."
             % (sum(len(u.members) for u, _ in units), len(units),
                one_line(source)),
             "-- Read it, then run it yourself. sdeindex runs nothing.",
             "-- A dropped index is gone. Script its CREATE INDEX first if you "
             "may want it back."]
    for unit, reason in units:
        lines.append("")
        lines.append("-- %s" % one_line(reason))
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
    unproven = sum(1 for r in results if r[0] == UNPROVEN)
    if refused:
        lines.append("VERDICT: %d of %d listed drop(s) refused. Do not run "
                     "this list as it stands." % (refused, len(results)))
    elif unproven:
        lines.append("VERDICT: no listed drop is refused, but %d of %d could "
                     "not be proven safe. Read every UNPROVEN line before "
                     "you run this list." % (unproven, len(results)))
    else:
        lines.append("VERDICT: all %d listed drop(s) safe" % len(results))
    return lines


def audit_units(model, results):
    """The SAFE units of an audit, in plan order, for the script."""
    safe = set(label for v, label, _ in results if v == SAFE)
    first = {}
    for _, label, why in results:
        first.setdefault(label, why)
    out = []
    for unit in model.units:
        if any(m.label in safe for m in unit.members):
            out.append((unit, first[unit.members[0].label]))
    return out


# --------------------------------------------------------------- the pure run

def decode(data, what):
    """Text from file bytes. SSMS saves UTF-8 with a BOM or UTF-16."""
    encoding = "utf-16" if data.startswith(
        (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        raise Refused("%s is not UTF-8 or UTF-16 text. Save it again as "
                      "UTF-8." % what)


def read_rows(text, needed, what):
    """[(line, row)] from CSV text. Header names fold to lower case."""
    reader = csv.DictReader(io.StringIO(text))
    try:
        names = reader.fieldnames
        if not names:
            raise Refused("%s is empty. It needs a header row." % what)
        names = [n.strip().lower() for n in names]
        if len(set(names)) != len(names):
            raise Refused("%s has a column name twice in its header." % what)
        missing = [n for n in needed if n not in names]
        if missing:
            raise Refused("%s has no %s column. The query --print-query "
                          "writes the columns this tool reads."
                          % (what, ", ".join(missing)))
        reader.fieldnames = names
        pairs = [(reader.line_num, row) for row in reader]
    except csv.Error as exc:
        raise Refused("%s is not readable CSV: %s" % (what, exc))
    if not pairs:
        raise Refused("%s has a header and no rows. Is the export empty?"
                      % what)
    return pairs


def analyse(index_text, registry_text=None, drops_text=None,
            min_days=DEFAULT_MIN_UPTIME_DAYS):
    """(report lines, exit code, [(unit, reason)] to script). No I/O.

    Raises Refused when the input cannot be judged."""
    pairs = read_rows(index_text, INDEX_COLUMNS, "the index export")
    uptime = uptime_hours([row for _, row in pairs])
    registry = None
    if registry_text is not None:
        registry = registry_from_rows(
            read_rows(registry_text, REGISTRY_COLUMNS, "the registry export"))
    model = build([index_from_row(row, line) for line, row in pairs],
                  registry)
    head = [describe_uptime(uptime, min_days), ""]
    if drops_text is None:
        decisions = plan(model, uptime, min_days)
        return (head + describe_plan(decisions), 0,
                [(u, why) for act, u, why in decisions if act == DROP])
    results = audit(model, parse_drops(drops_text), uptime, min_days)
    # An UNPROVEN drop exits 1 too: a gate that reads only the exit code must
    # not pass a drop the rules could not judge.
    code = 1 if any(r[0] != SAFE for r in results) else 0
    return head + describe_audit(results), code, audit_units(model, results)


# ----------------------------------------------------------------------- CLI

class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _days(text):
    if not whole(text):
        raise argparse.ArgumentTypeError("%r is not a whole number of days"
                                         % text)
    return int(text)


def _parse(argv):
    ap = _Parser(
        prog="sdeindex.py", allow_abbrev=False,
        description="Plan or audit an index cleanup for an enterprise "
                    "geodatabase in SQL Server. Nothing is written without "
                    "--apply and nothing is run against a server.")
    ap.add_argument("indexes", nargs="?",
                    help="CSV from the query --print-query writes")
    ap.add_argument("--registry", help="CSV of the geodatabase table "
                    "registry. Needed when a table is named a_<id> or d_<id>")
    ap.add_argument("--drops", help="a DROP INDEX / ALTER INDEX ... DISABLE "
                    "script to audit instead of planning one")
    ap.add_argument("--min-uptime-days", type=_days,
                    default=DEFAULT_MIN_UPTIME_DAYS,
                    help="days of usage counters an unused verdict needs "
                         "(default %d)" % DEFAULT_MIN_UPTIME_DAYS)
    ap.add_argument("--out", help="where --apply writes the DROP INDEX script")
    ap.add_argument("--apply", action="store_true",
                    help="write the script to --out. Without it nothing is "
                         "written")
    ap.add_argument("--print-query", action="store_true",
                    help="print the two export queries and exit")
    ap.add_argument("--self-test", action="store_true",
                    help="run the offline assertions and exit")
    args = ap.parse_args(argv)
    if not (args.self_test or args.print_query or args.indexes):
        ap.error("name the index export CSV, or use --print-query or "
                 "--self-test")
    if args.apply and not args.out:
        ap.error("--apply needs --out FILE")
    return args


def _quiet_encoding(streams=None):
    """An index name with a non-ASCII letter must not crash a cp1252 console."""
    for stream in streams or (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def read_text(path, what):
    with open(path, "rb") as handle:
        return decode(handle.read(), what)


def main(argv=None):
    _quiet_encoding()
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if args.self_test:
        return self_test()
    if args.print_query:
        print(INDEX_QUERY)
        print(REGISTRY_QUERY)
        return 0
    try:
        texts = [None if path is None else read_text(path, path)
                 for path in (args.indexes, args.registry, args.drops)]
        lines, code, units = analyse(texts[0], texts[1], texts[2],
                                     args.min_uptime_days)
    except Refused as exc:
        print("REFUSED: %s\nNothing was judged." % exc, file=sys.stderr)
        return 3
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    if not units:
        print("No drop script: nothing is safe to drop.")
        return code
    if not args.apply:
        print("Script not written: %d unit(s) to drop. Add --out FILE "
              "--apply to write it." % len(units))
        return code
    source = os.path.basename(args.drops) if args.drops else "the plan"
    try:
        # Mode "x" fails when the file exists, so an old script survives.
        with io.open(args.out, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(drop_script(units, source))
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("Wrote %d unit(s) to %s. Read it, then run it yourself."
          % (len(units), args.out))
    return code


# ------------------------------------------------------------------ self-test

class Recorder(object):
    """The check()/raises() harness, a class so the self-test can feed it
    failures and prove it reports them."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, text=""):
        try:
            fn()
        except Refused as exc:
            self.check(text in str(exc), "%s (message names %r)" % (
                label, text) if text else label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def finish(self):
        """Print the footer and return the exit code."""
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Assertions over the whole tool. No server, no network, no credentials."""
    rec = Recorder()
    check, raises = rec.check, rec.raises

    def mk(name, table="parcels", schema="gis", keys="[owner]", kind=JUDGED_TYPE,
           reads=0, writes=0, unique=False, pk=False, uc=False, includes="",
           flt="", line=0):
        return Index(schema, table, name, kind, unique, pk, uc,
                     split_columns(keys),
                     [n for n, _ in split_columns(includes)], flt, reads,
                     writes, line)

    def entries(*names, **kw):
        schema, table = kw.get("schema", "gis"), kw.get("table", "parcels")
        return [(schema, table, n, i + 1) for i, n in enumerate(names)]

    def verdicts(results):
        return dict((label.split(".")[-1], v) for v, label, _ in results)

    def actions(decisions):
        return [(a, u.members[0].name) for a, u, _ in decisions]

    registry = {7: ("gis", "parcels"), 9: ("gis", "roads")}
    mid = 30 * 24

    def export(rows, uptime=""):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        extra = [UPTIME_COLUMN] if uptime != "" else []
        writer.writerow(list(INDEX_COLUMNS) + extra)
        for r in rows:
            writer.writerow(list(r) + ([uptime] if extra else []))
        return out.getvalue()

    def xrow(table, name, keys="[owner] ASC", schema="gis", kind="NONCLUSTERED",
             uniq=0, pk=0, uc=0, inc="", flt="", seeks=0, scans=0, lookups=0,
             upd=0):
        return [schema, table, name, kind, uniq, pk, uc, keys, inc, flt,
                seeks, scans, lookups, upd]

    REG_CSV = "registration_id,owner,table_name\n7,gis,parcels\n"

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    print("sdeindex self-test: no server, no network, no credentials")
    print("-" * 68)

    # ---- reading a column list and a value
    check(split_columns("[Owner] ASC+[Zone] DESC") ==
          (("owner", "ASC"), ("zone", "DESC")),
          "a column list reads its order and folds case")
    check(split_columns("") == () and split_columns(None) == (),
          "an empty column list is an empty tuple")
    check(split_columns("owner+zone") == (("owner", "ASC"), ("zone", "ASC")),
          "a bare column name reads as ascending")
    check(split_columns("[a]]b] ASC") == (("a]b", "ASC"),),
          "a closing bracket inside a name is read as part of the name")
    raises(lambda: split_columns("[a] [b]"), "two columns with no plus are "
           "refused", "cannot read the column list")
    check(parse_flag("1", "f") is True and parse_flag(" True ", "f") is True,
          "1 and true read as true")
    check(parse_flag("0", "f") is False and parse_flag("false", "f") is False,
          "0 and false read as false")
    raises(lambda: parse_flag("yes", "is_unique"), "a flag that is not 1 or 0 "
           "is refused", "is_unique is 'yes'")
    check(parse_count("", "c") == 0 and parse_count("NULL", "c") == 0 and
          parse_count(None, "c") == 0, "a blank or NULL counter is 0")
    check(parse_count(" 42 ", "c") == 42, "a counter reads as a whole number")
    raises(lambda: parse_count("4.5", "user_seeks"), "a decimal counter is "
           "refused", "user_seeks is '4.5'")
    raises(lambda: parse_count("9" * 5000, "user_seeks"), "a counter of 5000 "
           "digits is refused, not a crash  <-- pinned defect",
           "19 digits or fewer")
    check(parse_count("9" * 19, "c") == 10 ** 19 - 1 and not whole("9" * 20),
          "19 digits is the widest counter")
    raises(lambda: parse_count(u"\u00b2", "user_seeks"), "a superscript digit "
           "is refused, not a crash  <-- pinned defect")
    check(whole("0042") and not whole("") and not whole("12\n") and
          not whole(u"\u0663"), "whole() accepts ASCII digits and nothing else")
    check(bracket("a]b") == "[a]]b]", "a name is bracketed with ] doubled")

    # ---- the Index value
    a = mk("ix_a", keys="[owner] ASC+[zone] DESC", includes="[b]+[a]",
           flt=" [x]  =  1 ", kind="nonclustered")
    check(a.label == "[gis].[parcels].[ix_a]" and a.kind == "NONCLUSTERED",
          "an index has a bracketed label and an upper-case type")
    check(a.signature == ("NONCLUSTERED", (("owner", "ASC"), ("zone", "DESC")),
                          ("a", "b"), "[x] = 1"),
          "the signature has the type, keys, sorted includes and a tidy filter")
    check(a.columns == set(["owner", "zone", "a", "b"]),
          "an index reports every column it touches")
    check(mk("x", keys="[owner] ASC").signature !=
          mk("x", keys="[owner] DESC").signature,
          "key direction is part of the signature")
    check(mk("x", flt="[a]=1").signature != mk("x").signature,
          "a filter is part of the signature")

    # ---- one export row
    good = dict(zip(INDEX_COLUMNS, [str(v) for v in xrow(
        "parcels", "ix_a", seeks=1, scans=2, lookups=3, upd=9)]))
    ix = index_from_row(good, 5)
    check(ix.reads == 6 and ix.writes == 9 and ix.line == 5,
          "reads add seeks, scans and lookups; writes are the updates")
    short = dict(good, user_updates=None)
    raises(lambda: index_from_row(short, 6), "a row with fewer fields than the "
           "header is refused", "line 6: the row has fewer fields")
    long_row = dict(good)
    long_row[None] = ["x"]
    raises(lambda: index_from_row(long_row, 7), "a row with more fields than "
           "the header is refused", "more fields")
    raises(lambda: index_from_row(dict(good, is_unique="x"), 8),
           "a bad flag names its line", "line 8")

    # ---- uptime and registry
    rows = [{UPTIME_COLUMN: "720"}, {UPTIME_COLUMN: "720"}]
    check(uptime_hours(rows) == 720, "one uptime on every row is read")
    check(uptime_hours([{}, {UPTIME_COLUMN: ""}]) is None,
          "no uptime column means no uptime")
    raises(lambda: uptime_hours([{UPTIME_COLUMN: "720"},
                                 {UPTIME_COLUMN: "5"}]),
           "two uptimes in one export are refused", "joins two")
    raises(lambda: uptime_hours([{UPTIME_COLUMN: "720"},
                                 {UPTIME_COLUMN: ""}]),
           "an uptime on some rows only is refused", "joins two")
    raises(lambda: uptime_hours([{UPTIME_COLUMN: "12.5"}]),
           "a fractional uptime is refused", "not a whole number of hours")
    raises(lambda: uptime_hours([{UPTIME_COLUMN: "9" * 5000}]),
           "an uptime of 5000 digits is refused, not a crash  "
           "<-- pinned defect", "not a whole number of hours")
    check(registry_from_rows([(2, {"registration_id": "7", "owner": " GIS ",
                                   "table_name": "Parcels"})]) ==
          {7: ("gis", "parcels")}, "the registry folds owner and table to "
          "lower case")
    raises(lambda: registry_from_rows([(2, {"registration_id": "x",
                                            "owner": "", "table_name": ""})]),
           "a registration id that is not a number is refused",
           "registry line 2")
    row7 = {"registration_id": "7", "owner": "gis", "table_name": "p"}
    raises(lambda: registry_from_rows([(2, row7), (3, row7)]),
           "a registration id listed twice is refused", "listed twice")
    check(registry_from_rows([(2, {"registration_id": "7", "owner": None,
                                   "table_name": None})]) == {7: ("", "")},
          "a blank registry owner and table read as empty")

    # ---- protections
    def why(**kw):
        return protection(mk("ix", **kw))

    check(why() is None, "a plain nonclustered index is judged")
    check("sde schema" in why(schema="SDE"),
          "an index in the sde schema is protected")
    check("system table" in why(table="SDE_states"),
          "an index on an SDE_ table is protected")
    check("system table" in why(table="gdb_items"),
          "an index on a GDB_ table is protected")
    check(protection(mk("R7_SDE_ROWID_UK")) is not None and
          "ObjectID" in protection(mk("r7_sde_rowid_uk")),
          "the ObjectID index is protected")
    check("PRIMARY KEY" in why(pk=True), "a primary key index is protected")
    check("UNIQUE constraint" in why(uc=True),
          "a unique constraint index is protected")
    check("never see" in why(kind="SPATIAL") and
          "never see" in why(kind="NONCLUSTERED HASH"),
          "spatial and hash indexes are protected, the counters never see them")
    check("does not judge" in why(kind="CLUSTERED") and
          "does not judge" in why(kind="CLUSTERED COLUMNSTORE"),
          "a clustered index is protected, the tool judges nonclustered only")
    check("enforces a rule" in why(unique=True),
          "a plain unique index is protected")
    check(protection(mk("ix"), "is a delta") == "is a delta",
          "a reason passed in from the delta rules wins")

    # ---- the geodatabase: adds and deletes tables
    def cls(name, table, reg=registry, **kw):
        return classify(mk(name, table=table, **kw), reg)

    check(cls("ix", "parcels") == (("gis", "parcels"), None),
          "an index on an ordinary table is its own home")
    check(cls("ix", "a_7")[0] == ("gis", "parcels") and
          cls("ix", "a_7")[1] is None,
          "an adds-table index lives at its base table")
    check(cls("ix", "A7")[0] == ("gis", "parcels"),
          "the older a7 spelling and upper case are read")
    check("deletes table" in cls("ix", "d_7")[1] and
          cls("ix", "d_7")[0] == ("gis", "d_7"),
          "a deletes-table index is protected and stays on its own table")
    check("maintains" in cls("ix", "a_7", keys="[sde_state_id]")[1],
          "an adds-table index on sde_state_id is protected")
    check("no registry row" in cls("ix", "a_99")[1],
          "a delta-named table with no registry row is protected")
    check("no registry row" in cls("ix", "a_7", schema="other")[1],
          "a delta-named table owned by another schema is protected")
    check("no registry row in schema gis owns id 7" in
          cls("ix", "a_7", reg={7: ("other", "a_7")})[1],
          "a table a_7 in schema gis is not owned by a registry row for a_7 "
          "under another owner  <-- pinned defect")
    check("no registry row" in cls("ix", "a" + "9" * 5000)[1],
          "a delta-named table with 5000 digits is protected, not a crash  "
          "<-- pinned defect")
    check(cls("ix", "d_5", reg={5: ("gis", "d_5")}) ==
          (("gis", "d_5"), None),
          "a table registered under the name d_5 is not a deletes table  "
          "<-- pinned defect")
    raises(lambda: cls("ix", "a_7", reg=None), "a delta-named table with no "
           "registry is refused", "no --registry was given")

    # ---- grouping and twins
    base, adds = mk("ix_zone", keys="[zone]"), mk("ix_zone", "a_7", keys="[zone]")
    model = build([base, adds], registry)
    check(len(model.units) == 1 and len(model.units[0].members) == 2,
          "a base index and its adds-table twin are one unit")
    check(" + twin " in model.units[0].label, "a twin unit names both members")
    check(model.find(None, "A_7", "IX_ZONE") == [adds] and
          model.find("GIS", "a_7", "ix_zone") == [adds] and
          model.find("other", "a_7", "ix_zone") == [],
          "find folds case and filters by schema")
    odd = build([mk("ix_zone", keys="[zone]"),
                 mk("ix_zone", "a_7", keys="[zone]+[owner]")], registry)
    check(len(odd.units) == 2, "an adds index with other keys is not a twin")
    three = build([mk("ix_1"), mk("ix_2"), mk("ix_1", "a_7")], registry)
    check(sorted(len(u.members) for u in three.units) == [1, 2],
          "two identical base indexes and one adds twin make a pair and a "
          "single")
    check(three.groups[0][0].members[0].name == "ix_1" and
          len(three.groups[0][0].members) == 2,
          "the paired unit is the one a group keeps")
    raises(lambda: build([mk("ix", line=2), mk("IX", line=9)]),
           "the same index twice is refused", "line 2 and line 9")
    raises(lambda: build([mk("IX_A"), mk("ix_a")]), "two names that differ "
           "only in case are refused with a message that says so  "
           "<-- pinned defect", "differ only in case")

    # ---- the plan
    pair = build([mk("ix_a"), mk("ix_b")])
    check(actions(plan(pair, mid)) == [(REVIEW, "ix_a"), (DROP, "ix_b")],
          "of two identical unread indexes the plan keeps one for review and "
          "drops the other  <-- pinned defect")
    check(actions(plan(pair, None)) == [(HOLD, "ix_a"), (DROP, "ix_b")],
          "with no uptime the unread keeper is held, the copy still drops")
    solo = build([mk("ix_a")])
    check(actions(plan(solo, 14 * 24)) == [(DROP, "ix_a")],
          "an unread index drops at exactly the floor")
    check(actions(plan(solo, 14 * 24 - 1)) == [(HOLD, "ix_a")],
          "an unread index is held one hour under the floor  <-- pinned defect")
    check(actions(plan(solo, None)) == [(HOLD, "ix_a")],
          "an unread index is held when the export has no uptime")
    check(actions(plan(solo, 5 * 24, 3)) == [(DROP, "ix_a")],
          "the floor is a setting")
    check(actions(plan(build([mk("ix_a", reads=3)]), None)) ==
          [(KEEP, "ix_a")], "a read index is kept")
    dup_read = build([mk("ix_a"), mk("ix_b", reads=4)])
    check(actions(plan(dup_read, None)) == [(KEEP, "ix_b"), (DROP, "ix_a")],
          "of two identical indexes the read one stays and the other drops")
    pk_dup = build([mk("ix_pk", pk=True), mk("ix_copy")])
    check(actions(plan(pk_dup, None)) == [(KEEP, "ix_pk"), (DROP, "ix_copy")],
          "a copy of a primary key index drops and the key stays")
    two_unique = build([mk("u1", unique=True), mk("u2", unique=True)])
    check(actions(plan(two_unique, None)) == [(KEEP, "u1"), (KEEP, "u2")],
          "two identical unique indexes are both kept")
    check(actions(plan(build([mk("ix_1"), mk("ix_1", "a_7"),
                              mk("ix_2", reads=5)], registry), mid)) ==
          [(KEEP, "ix_1"), (DROP, "ix_2")],
          "a read on any copy keeps the kept copy, whatever it was read on  "
          "<-- pinned defect")
    twin_model = build([mk("ix_zone"), mk("ix_zone", "a_7")], registry)
    twin_plan = plan(twin_model, mid)
    check(len(twin_plan) == 1 and twin_plan[0][0] == DROP and
          len(twin_plan[0][1].members) == 2,
          "an unread base index and its twin drop as one unit")
    check(actions(plan(build([mk("ix_zone"),
                              mk("ix_zone", "a_7", reads=2)], registry),
                       mid)) == [(KEEP, "ix_zone")],
          "a read on the adds twin keeps the pair  <-- pinned defect")
    text = "\n".join(describe_plan(twin_plan))
    check("DROP" in text and "VERDICT: 1 drop(s) planned, 0 for review, 0 "
          "held for uptime" in text, "the plan report ends in a verdict")
    check("KEEP      0 unit(s)" in text, "the plan report counts the kept")
    mix = "\n".join(describe_plan(plan(build(
        [mk("ix_a"), mk("ix_b"), mk("ix_r", keys="[r]", reads=1),
         mk("ix_pk", keys="[p]", pk=True), mk("ix_h", keys="[h]")]), 400)))
    check("REVIEW" in mix and "KEEP      2 unit(s): 1 protected by a rule, "
          "1 read" in mix, "the plan report splits the kept by reason")
    check(describe_plan(twin_plan) == [
        "DROP      [gis].[parcels].[ix_zone] + twin [gis].[a_7].[ix_zone]  "
        "no reads in 30.0 days of uptime, 0 write(s) paid for nothing",
        "KEEP      0 unit(s): 0 protected by a rule, 0 read", "",
        "VERDICT: 1 drop(s) planned, 0 for review, 0 held for uptime"],
        "each decision is printed once, under its own action  "
        "<-- pinned defect")
    pk_twin = build([mk("ix_pk", keys="[zone]", pk=True),
                     mk("ix_pk", "a_7", keys="[zone]")], registry)
    check(actions(plan(pk_twin, mid)) == [(KEEP, "ix_pk")] and
          pk_twin.units[0].protected,
          "a twin pair is protected when only one member is  <-- pinned "
          "defect")
    check(classify(mk("ix", keys="[sde_state_id]"), registry)[1] is None,
          "an index over sde_state_id on a base table is judged, only the "
          "delta tables are protected by column")
    held = "\n".join(describe_plan(plan(build([mk("ix_a")]), 10)))
    check("HOLD" in held and "1 held for uptime" in held,
          "the plan report shows a held index")

    # ---- the audit
    def aud(ixs, names, up=mid, reg=None, days=DEFAULT_MIN_UPTIME_DAYS,
            **kw):
        m = build(ixs, reg)
        return audit(m, entries(*names, **kw), up, days), m

    r, _ = aud([mk("ix_a"), mk("ix_b")], ["ix_a", "ix_b"])
    check(verdicts(r) == {"[ix_a]": REFUSED, "[ix_b]": SAFE},
          "a list that drops both of an identical pair is REFUSED on one of "
          "them  <-- pinned defect")
    check("every copy" in r[0][2] and "ix_a" in r[1][2],
          "the refusal says why and the safe drop names what stays")
    r2, _ = aud([mk("ix_a"), mk("ix_b")], ["ix_b", "ix_a"])
    check(verdicts(r2) == verdicts(r),
          "the refused copy does not depend on the order of the list  "
          "<-- pinned defect")
    r3, _ = aud([mk("ix_a"), mk("ix_b"), mk("ix_c")], ["ix_a", "ix_b"])
    check(verdicts(r3) == {"[ix_a]": SAFE, "[ix_b]": SAFE},
          "two of three identical copies may go, one stays")
    r4, _ = aud([mk("ix_a"), mk("ix_b"), mk("ix_c")],
                ["ix_a", "ix_b", "ix_c"])
    check(sorted(verdicts(r4).values()) == [REFUSED, SAFE, SAFE],
          "all three identical copies listed: one is refused")
    r5, _ = aud([mk("ix_a"), mk("ix_b", reads=9)], ["ix_a"])
    check(verdicts(r5) == {"[ix_a]": SAFE},
          "a copy of a read index is safe to drop")
    r6, _ = aud([mk("ix_a")], ["ix_a"])
    check(verdicts(r6) == {"[ix_a]": SAFE} and "uptime" in r6[0][2],
          "an unread index alone is safe past the floor")
    r7, _ = aud([mk("ix_a")], ["ix_a"], up=100)
    check(verdicts(r7) == {"[ix_a]": REFUSED} and "4.2 days" in r7[0][2],
          "an unread index alone is refused under the floor  "
          "<-- pinned defect")
    r8, _ = aud([mk("ix_a")], ["ix_a"], up=None)
    check(verdicts(r8) == {"[ix_a]": REFUSED}, "no uptime refuses an unread "
          "index")
    r9, _ = aud([mk("ix_a", reads=7)], ["ix_a"])
    check(verdicts(r9) == {"[ix_a]": UNPROVEN} and "7 time(s)" in r9[0][2],
          "a read index nothing duplicates is UNPROVEN, not refused")
    r10, _ = aud([mk("ix_a")], ["ix_a", "ix_a"])
    check([v for v, _, _ in r10] == [SAFE, SAFE], "an index listed twice is "
          "judged twice")
    r11, _ = aud([mk("ix_pk", pk=True), mk("ix_copy")], ["ix_pk", "ix_copy"])
    check(verdicts(r11) == {"[ix_pk]": REFUSED, "[ix_copy]": SAFE} and
          "PRIMARY KEY" in r11[0][2],
          "a listed primary key is refused and its copy is safe")
    r12, _ = aud([mk("r7_sde_rowid_uk", unique=True)], ["r7_sde_rowid_uk"])
    check(verdicts(r12) == {"[r7_sde_rowid_uk]": REFUSED},
          "a listed ObjectID index is refused")
    r13, _ = aud([mk("ix_zone"), mk("ix_zone", "a_7")], ["ix_zone"],
                 reg=registry)
    check(verdicts(r13) == {"[ix_zone]": REFUSED} and
          "leaves its twin [gis].[a_7].[ix_zone]" in r13[0][2],
          "a base index listed without its adds twin is refused  "
          "<-- pinned defect")
    r14, m14 = aud([mk("ix_zone"), mk("ix_zone", "a_7")], ["ix_zone"],
                   reg=registry)
    r14b = audit(m14, entries("ix_zone", table="parcels") +
                 [("gis", "a_7", "ix_zone", 2)], mid)
    check([v for v, _, _ in r14b] == [SAFE, SAFE],
          "a base index listed with its adds twin is safe")
    check(len(audit_units(m14, r14b)) == 1 and
          len(audit_units(m14, r14b)[0][0].members) == 2,
          "the script for a twin pair holds one unit with both drops")
    check(audit_units(m14, r14) == [], "a refused twin makes no script")
    r14c = audit(pk_twin, [("gis", "a_7", "ix_pk", 1)], mid)
    check(r14c[0][0] == REFUSED and "its twin [gis].[parcels].[ix_pk] backs "
          "a PRIMARY KEY" in r14c[0][2],
          "an unprotected twin listed alone is refused for its twin's rule")
    r14d = audit(pk_twin, [("gis", "parcels", "ix_pk", 1)], mid)
    check(r14d[0][0] == REFUSED and r14d[0][2] == "backs a PRIMARY KEY constraint",
          "a protected twin listed alone is refused for its own rule")
    r15, _ = aud([mk("ix_zone", keys="[owner]"),
                  mk("ix_x", "a_7", keys="[zone]")], ["ix_x"],
                 reg=registry, table="a_7")
    check(verdicts(r15) == {"[ix_x]": SAFE},
          "an adds index with no twin on the base table is judged alone")
    r16, _ = aud([mk("ix_d", "d_7")], ["ix_d"], reg=registry, table="d_7")
    check(verdicts(r16) == {"[ix_d]": REFUSED},
          "a listed deletes-table index is refused")
    r17, _ = aud([mk("ix_a")], ["ix_missing"])
    check(verdicts(r17) == {"[ix_missing]": REFUSED} and
          r17[0][1] == "[gis].[parcels].[ix_missing]" and
          "not in the index export" in r17[0][2] and "line 1" in r17[0][2],
          "an index missing from the export is refused with a clean label  "
          "<-- pinned defect")
    r18 = audit(build([mk("ix_a"), mk("ix_a", schema="hr")]),
                [(None, "parcels", "ix_a", 3)], mid)
    check(r18[0][0] == REFUSED and "more than one schema" in r18[0][2] and
          r18[0][1] == "[parcels].[ix_a]",
          "an unqualified name in two schemas is refused")
    r19 = audit(build([mk("ix_a"), mk("ix_a", schema="hr")]),
                [("hr", "parcels", "ix_a", 3)], mid)
    check(r19[0][0] == SAFE, "naming the schema picks the one index")
    out = "\n".join(describe_audit(r))
    check("VERDICT: 1 of 2 listed drop(s) refused. Do not run this list as "
          "it stands." in out, "an audit report with a refusal says not to "
          "run the list")
    out = "\n".join(describe_audit(r3))
    check("VERDICT: all 2 listed drop(s) safe" in out,
          "a clean audit report counts what it judged")
    out = "\n".join(describe_audit(r9 + r3))
    check("VERDICT: no listed drop is refused, but 1 of 3 could not be proven "
          "safe. Read every UNPROVEN line" in out,
          "an audit report with an unproven drop says to read it")
    check(describe_uptime(14 * 24, 14) ==
          "uptime: 14.0 days of usage counters, floor 14 days" and
          describe_uptime(14 * 24 - 1, 14).endswith(
              ", so no index is called unused"),
          "the uptime line changes at exactly the floor")
    check("uptime: 30.0 days of usage counters, floor 14 days" ==
          describe_uptime(mid, 14) and
          describe_uptime(None, 14).endswith("so no index is called unused") and
          describe_uptime(100, 14).endswith("so no index is called unused"),
          "the uptime line says when no index can be called unused")

    # ---- reading a DROP list
    check(parse_drops("DROP INDEX [ix_a] ON [gis].[parcels];") ==
          [("gis", "parcels", "ix_a", 1)], "a bracketed drop is read")
    check(parse_drops('drop index if exists "ix""a" on "gis"."parcels"') ==
          [("gis", "parcels", 'ix"a', 1)],
          "lower case, IF EXISTS and double quotes are read")
    check(parse_drops("DROP INDEX ix ON parcels")[0][:3] ==
          (None, "parcels", "ix"), "a table with no schema reads as None")
    check(parse_drops("DROP INDEX ix ON db.gis.parcels")[0][:3] ==
          ("gis", "parcels", "ix"), "a three-part table name keeps the schema")
    got = parse_drops("-- DROP INDEX no ON t\n/* DROP INDEX no\n ON t */\n"
                      "DROP INDEX yes ON gis.t;")
    check(got == [("gis", "t", "yes", 4)],
          "comments are ignored and line numbers survive them  "
          "<-- pinned defect")
    check(parse_drops("ALTER INDEX [ix_a] ON [gis].[parcels] DISABLE;")[0] ==
          ("gis", "parcels", "ix_a", 1),
          "ALTER INDEX ... DISABLE is read as a removal, as sp_IndexCleanup "
          "scripts it")
    check(parse_drops("ALTER INDEX a ON gis.t REBUILD;\n"
                      "ALTER INDEX b ON gis.t REBUILD\n"
                      "DROP INDEX c ON gis.t\n"
                      "ALTER INDEX d ON gis.t DISABLE")
          == [("gis", "t", "c", 3), ("gis", "t", "d", 4)],
          "a rebuild is not a removal  <-- pinned defect")
    raises(lambda: parse_drops("ALTER INDEX ALL ON gis.t DISABLE;"),
           "ALTER INDEX ALL ... DISABLE is refused", "line 1")
    raises(lambda: parse_drops("ALTER INDEX a ON DISABLE;"),
           "an unreadable ALTER INDEX is refused, never skipped",
           "ALTER INDEX name ON schema.table DISABLE")
    raises(lambda: parse_drops("DROP INDEX gis.parcels.ix"),
           "the old table.index form is refused", "DROP INDEX name ON")
    raises(lambda: parse_drops("DROP INDEX a ON t, b ON t"),
           "one DROP naming two indexes is refused", "several indexes")
    raises(lambda: parse_drops("DROP INDEX a ON gis.t WITH (ONLINE = ON), "
                               "b ON gis.t WITH (ONLINE = ON);"),
           "a second index after a WITH clause is refused, not skipped  "
           "<-- pinned defect", "several indexes")
    raises(lambda: parse_drops("DROP INDEX a ON gis.t WITH (ONLINE = ON"),
           "a WITH clause that never closes is refused", "never closes")
    check(parse_drops("DROP INDEX a ON gis.t WITH (ONLINE = ON (WAIT_AT_LOW_"
                      "PRIORITY (MAX_DURATION = 1)));") ==
          [("gis", "t", "a", 1)],
          "a WITH clause with nested brackets is read past")
    raises(lambda: parse_drops("SELECT 1"), "a file with no statement is "
           "refused", "holds no DROP INDEX")
    check(parse_drops("ALTER TABLE [db].[gis].[parcels] DROP CONSTRAINT "
                      "[uq_x];") == [("gis", "parcels", "uq_x", 1)],
          "ALTER TABLE ... DROP CONSTRAINT is read as a removal, as "
          "sp_IndexCleanup scripts it  <-- pinned defect")
    check(parse_drops("alter table gis.t drop constraint if exists uq_x")[0] ==
          ("gis", "t", "uq_x", 1), "IF EXISTS and lower case are read on a "
          "constraint drop")
    raises(lambda: parse_drops("ALTER TABLE gis.t DROP CONSTRAINT a, b;"),
           "a constraint drop naming two constraints is refused",
           "several indexes")
    raises(lambda: parse_drops("ALTER TABLE gis.t DROP CONSTRAINT ;"),
           "an unreadable constraint drop is refused, never skipped",
           "ALTER TABLE schema.table DROP CONSTRAINT name")
    raises(lambda: parse_drops("CREATE NONCLUSTERED INDEX a ON gis.t ([z]) "
                               "WITH (DROP_EXISTING = ON);\nDROP INDEX b ON "
                               "gis.t;"),
           "a CREATE INDEX that rewrites an index is refused, not skipped  "
           "<-- pinned defect", "DROP_EXISTING")
    check(parse_drops("create unique clustered columnstore index a on gis.t "
                      "(z);\nCREATE INDEX c ON gis.t (y) WITH (ONLINE = ON)\n"
                      "DROP INDEX b ON gis.t;") == [("gis", "t", "b", 3)],
          "a CREATE INDEX that rewrites nothing is skipped, in any case")
    raises(lambda: parse_drops("DROP INDEX b ON gis.t;\ncreate index a on "
                               "gis.t (z) with (drop_existing=on)"),
           "the rewrite is found after a drop and in lower case", "line 2")
    raises(lambda: parse_drops("DROP TABLE gis.a_7;"), "DROP TABLE is refused, "
           "it removes every index on the table  <-- pinned defect",
           "DROP TABLE removes every index")
    check(parse_drops("ALTER TABLE gis.t ADD c int;\n"
                      "ALTER TABLE gis.t DISABLE TRIGGER ALL;\n"
                      "DROP INDEX a ON gis.t;") == [("gis", "t", "a", 3)],
          "other ALTER TABLE statements are not removals")
    check(parse_drops("ALTER INDEX a ON gis.t REBUILD\n"
                      "ALTER TABLE gis.t DISABLE TRIGGER ALL;\n"
                      "DROP INDEX b ON gis.t;") == [("gis", "t", "b", 3)],
          "a rebuild with no semicolon before ALTER TABLE ... DISABLE is not a "
          "removal  <-- pinned defect")
    check(parse_drops("ALTER INDEX a ON gis.t REBUILD\n"
                      "ALTER INDEX b ON gis.t DISABLE\n"
                      "DROP INDEX c ON gis.t") ==
          [("gis", "t", "b", 2), ("gis", "t", "c", 3)],
          "a rebuild with no semicolon does not swallow the next ALTER INDEX  "
          "<-- pinned defect")
    check(parse_drops("\nDROP INDEX a ON gis.t")[0][3] == 2,
          "a DROP list that starts with a blank line numbers its lines from 1")
    # Comment starts inside a string, a bracketed name or another comment.
    check(parse_drops("PRINT '--'; DROP INDEX ix_a ON gis.parcels; "
                      "DROP INDEX ix_b ON gis.parcels;") ==
          [("gis", "parcels", "ix_a", 1), ("gis", "parcels", "ix_b", 1)],
          "a '--' inside a string does not hide the DROP after it  "
          "<-- pinned defect")
    check(parse_drops("PRINT '/*'; DROP INDEX ix_a ON gis.t;")[0][2] == "ix_a",
          "a '/*' inside a string does not open a comment  <-- pinned defect")
    check([d[2] for d in parse_drops("DROP INDEX [a--b] ON gis.t; "
                                     "DROP INDEX c ON gis.t;")] ==
          ["a--b", "c"], "a '--' inside a bracketed name does not hide the "
          "DROP after it  <-- pinned defect")
    check(parse_drops("/* a /* DROP INDEX x ON t */ DROP INDEX y ON t */\n"
                      "DROP INDEX z ON gis.t") == [("gis", "t", "z", 2)],
          "block comments nest, as T-SQL nests them  <-- pinned defect")
    check(parse_drops("-- it's a comment\nDROP INDEX a ON gis.t")[0][3] == 2,
          "a quote inside a comment does not open a string")
    check(parse_drops("PRINT 'it''s'; DROP INDEX a ON gis.t")[0][2] == "a",
          "a doubled quote stays inside its string")
    raises(lambda: parse_drops("DROP INDEX a ON t;\n\n\nPRINT 'open"),
           "an unclosed string past line 2 names its own line  <-- pinned "
           "defect", "line 4:")
    check(parse_drops("ALTER INDEX a ON gis.t REBUILD\n"
                      "ALTER INDEX b ON gis.t DISABLE") == [("gis", "t", "b", 2)],
          "a rebuild next to a disable at the end of the file is not read as "
          "one statement  <-- pinned defect")
    check([d[2] for d in parse_drops("DROP INDEX a ON gis.t;\r-- note\r"
                                     "DROP INDEX b ON gis.t;\r")] == ["a", "b"],
          "a '--' comment ends at a bare CR, so the DROP after it is read  "
          "<-- pinned defect")
    for bad in ("PRINT 'open; DROP INDEX a ON gis.t", "/* open\nDROP INDEX a "
                "ON gis.t", "DROP INDEX [a ON gis.t", 'DROP INDEX "a ON t'):
        raises(lambda: parse_drops(bad), "a string, name or comment that "
               "never closes is refused: %s" % bad.split()[0], "never closes")

    # ---- the script
    script = drop_script([(twin_model.units[0], "reason one")], "a.sql")
    check(script.count("DROP INDEX") == 2 and "twin pair" in script and
          "-- reason one" in script and
          "DROP INDEX [ix_zone] ON [gis].[parcels];" in script and
          "DROP INDEX [ix_zone] ON [gis].[a_7];" in script,
          "the script drops both members of a twin pair")
    check(script.splitlines()[0].startswith("-- sdeindex drop script: 2 "
                                            "index(es) in 1 unit(s)"),
          "the script says how many it drops")
    evil = build([mk("ix\nDROP TABLE gis.parcels;")])
    script = drop_script([(evil.units[0], "identical to " + evil.units[0].label)],
                         "a\r\nDROP TABLE x.sql")
    check("DROP TABLE gis.parcels;" not in script.splitlines() and
          script.splitlines()[0].endswith("from a\\r\\nDROP TABLE x.sql.") and
          "-- identical to [gis].[parcels].[ix\\nDROP TABLE gis.parcels;]" in
          script,
          "a line break in a name or a file name cannot end a comment  "
          "<-- pinned defect")
    single = drop_script([(solo.units[0], "r")], "the plan")
    check("twin pair" not in single and single.count("DROP INDEX") == 1,
          "a single index has no twin comment")

    # ---- reading CSV text
    raises(lambda: read_rows("", INDEX_COLUMNS, "the export"),
           "an empty file is refused", "needs a header row")
    raises(lambda: read_rows("a,a\n1,2\n", ("a",), "the export"),
           "a header with a name twice is refused", "name twice")
    raises(lambda: read_rows("a,b\n1,2\n", ("a", "c"), "the export"),
           "a missing column is named", "no c column")
    raises(lambda: read_rows("a,b\n", ("a",), "the export"),
           "a header with no rows is refused", "no rows")
    raises(lambda: read_rows("a\n" + "x" * 200000 + "\n", ("a",), "the export"),
           "a field past the csv limit is refused, not a traceback",
           "not readable CSV")
    got = read_rows(" A ,B\n1,2\n\n3,4\n", ("a", "b"), "x")
    check(got[0][0] == 2 and got[1][0] == 4 and got[1][1]["b"] == "4",
          "headers fold case, blank lines are skipped, lines are counted")
    check(decode(u"\ufeffab".encode("utf-8"), "f") == "ab",
          "a UTF-8 BOM is dropped")
    check(decode(u"ab".encode("utf-16"), "f") == "ab",
          "a UTF-16 file reads")
    raises(lambda: decode(b"\xff\xfe\x00", "f"), "a broken UTF-16 file is "
           "refused", "not UTF-8 or UTF-16")
    raises(lambda: decode(b"caf\xe9", "f"), "a Latin-1 file is refused with "
           "the fix", "Save it again as UTF-8")

    # ---- the whole run, no files
    story = export([
        xrow("parcels", "ix_owner_1"), xrow("parcels", "ix_owner_2"),
        xrow("parcels", "ix_status", keys="[status] ASC", seeks=40),
        xrow("parcels", "ix_zone", keys="[zone] ASC", upd=5),
        xrow("a_7", "ix_zone", keys="[zone] ASC", upd=5),
        xrow("parcels", "pk_parcels", keys="[objectid] ASC", pk=1, uniq=1),
        xrow("a_7", "a7_idx1", keys="[sde_state_id] ASC"),
        xrow("d_7", "d7_idx1", keys="[sde_deletes_row_id] ASC"),
        xrow("SDE_states", "states_idx", schema="sde"),
    ], "720")
    lines, code, units = analyse(story, REG_CSV)
    body = "\n".join(lines)
    check(code == 0 and "VERDICT: 2 drop(s) planned, 1 for review, 0 held "
          "for uptime" in body, "a plan over a geodatabase export reads "
          "cleanly")
    check(len(units) == 2 and sum(len(u.members) for u, _ in units) == 3,
          "the plan scripts the copy and the twin pair, three drops")
    check(lines[0].startswith("uptime: 30.0 days"), "the plan opens with the "
          "uptime")
    naive = ("DROP INDEX ix_owner_1 ON gis.parcels;\n"
             "DROP INDEX ix_owner_2 ON gis.parcels;\n"
             "DROP INDEX ix_zone ON gis.parcels;\n"
             "DROP INDEX ix_status ON gis.parcels;\n")
    lines, code, units = analyse(story, REG_CSV, naive)
    body = "\n".join(lines)
    check(code == 1 and "VERDICT: 2 of 4 listed drop(s) refused" in body,
          "the list that drops both copies and half a twin exits 1")
    check(len(units) == 1 and units[0][0].members[0].name == "ix_owner_2",
          "the script keeps only the drop the rules allow")
    lines, code, units = analyse(story, REG_CSV,
                                 "DROP INDEX ix_owner_2 ON gis.parcels;")
    check(code == 0 and len(units) == 1, "a clean list exits 0")
    three = export([xrow("parcels", "ix_a"),
                    xrow("parcels", "ix_b", keys="[z] ASC"),
                    xrow("parcels", "ix_c", keys="[y] ASC")], "720")
    lines, code, units = analyse(three, None,
                                 "DROP INDEX ix_b ON gis.parcels;\n"
                                 "DROP INDEX nope ON gis.parcels;\n"
                                 "DROP INDEX ix_c ON gis.parcels;\n")
    check(code == 1 and "REFUSED   [gis].[parcels].[nope]  line 2: [nope] is "
          "not in the index export" in "\n".join(lines),
          "a missing name in the middle of a list is refused with its own "
          "label and line  <-- pinned defect")
    check(len(units) == 2, "the two listed indexes around it are still judged")
    pair = [xrow("zz", "ix1", keys="[z] ASC"), xrow("aa", "ix2", keys="[a] ASC")]
    check(analyse(export(pair, "720"))[0] ==
          analyse(export(pair[::-1], "720"))[0],
          "the plan reads the same for a reordered export  <-- pinned defect")
    check(analyse(export(pair, "720"), None, "DROP INDEX ix1 ON gis.zz;\n"
                  "DROP INDEX ix2 ON gis.aa;")[0] == analyse(
                      export(pair[::-1], "720"), None,
                      "DROP INDEX ix1 ON gis.zz;\nDROP INDEX ix2 ON gis.aa;")[0],
          "an audit reads the same for a reordered export")
    cons = export([
        xrow("parcels", "uq_parcel_id", keys="[parcel_id] ASC", uniq=1, uc=1,
             seeks=900),
        xrow("parcels", "ix_x", keys="[x] ASC", seeks=5)], "720")
    lines, code, units = analyse(cons, None,
        "ALTER TABLE [db].[gis].[parcels] DROP CONSTRAINT [uq_parcel_id];\n"
        "ALTER INDEX [ix_x] ON [db].[gis].[parcels] DISABLE;")
    body = "\n".join(lines)
    check(code == 1 and "REFUSED   [gis].[parcels].[uq_parcel_id]  backs a "
          "UNIQUE constraint" in body and "UNPROVEN  [gis].[parcels].[ix_x]"
          in body and units == [],
          "a DROP CONSTRAINT on a read unique constraint is refused, not "
          "skipped  <-- pinned defect")
    lines, code, units = analyse(cons, None, "DROP INDEX ix_x ON gis.parcels;")
    check(code == 1 and units == [] and "could not be proven safe" in
          lines[-1],
          "an audit with an unproven drop exits 1  <-- pinned defect")
    raises(lambda: analyse(story, REG_CSV, "DROP INDEX ix_owner_2 ON "
                           "gis.parcels;\nDROP TABLE gis.a_7;"),
           "a list with a DROP TABLE is refused whole", "DROP TABLE")
    raises(lambda: analyse(story, REG_CSV, "CREATE NONCLUSTERED INDEX "
                           "ix_owner_1 ON gis.parcels ([zone] ASC) WITH "
                           "(DROP_EXISTING = ON);\nDROP INDEX ix_owner_2 ON "
                           "gis.parcels;"),
           "an audit of a merge script is refused whole, not judged against "
           "an index the script rewrites  <-- pinned defect", "DROP_EXISTING")
    raises(lambda: analyse(story), "a geodatabase export with adds tables and "
           "no registry is refused", "no --registry was given")
    raises(lambda: analyse("schema_name\nx\n"), "an export missing columns is "
           "refused", "no table_name")
    raises(lambda: analyse(story, "registration_id\n7\n"), "a registry "
           "missing columns is refused", "the registry export has no")
    plain = export([xrow("parcels", "ix_a")])
    check(analyse(plain)[0][0].startswith("uptime: not in the export"),
          "an export with no uptime column says so")

    # ---- the command line
    work = tempfile.mkdtemp(prefix="sdeindex_")
    try:
        def put(name, data):
            path = os.path.join(work, name)
            with open(path, "wb") as handle:
                handle.write(data if isinstance(data, bytes)
                             else data.encode("utf-8"))
            return path
        idx = put("indexes.csv", story)
        reg = put("registry.csv", REG_CSV)
        lst = put("naive.sql", naive)
        out_sql = os.path.join(work, "drop.sql")

        code, out, err = run([idx, "--registry", reg])
        check(code == 0 and "VERDICT: 2 drop(s) planned" in out and
              "Script not written: 2 unit(s)" in out and err == "",
              "the plan prints and says the script was not written")
        code, out, err = run([idx, "--registry", reg, "--out", out_sql])
        check(code == 0 and not os.path.exists(out_sql),
              "--out without --apply writes nothing  <-- pinned defect")
        code, out, err = run([idx, "--registry", reg, "--out", out_sql,
                              "--apply"])
        with open(out_sql) as handle:
            written = handle.read()
        check(code == 0 and written.count("DROP INDEX") == 3 and
              "from the plan" in written and "Wrote 2 unit(s)" in out,
              "--apply writes the planned drops")
        code, out, err = run([idx, "--registry", reg, "--out", out_sql,
                              "--apply"])
        with open(out_sql) as handle:
            again = handle.read()
        check(code == 2 and again == written and err.startswith("error:"),
              "--apply will not overwrite a script that exists  "
              "<-- pinned defect")
        code, out, err = run([idx, "--registry", reg, "--drops", lst])
        check(code == 1 and "REFUSED" in out and "SAFE" in out and
              "UNPROVEN" in out, "an audit with a refusal exits 1")
        out2 = os.path.join(work, "safe.sql")
        code, out, err = run([idx, "--registry", reg, "--drops", lst, "--out",
                              out2, "--apply"])
        with open(out2) as handle:
            safe = handle.read()
        check(code == 1 and safe.count("DROP INDEX") == 1 and
              "ix_owner_2" in safe and "from naive.sql" in safe,
              "an audit writes only the safe drops, and names its source")
        both = put("both.sql", "DROP INDEX ix_status ON gis.parcels;")
        code, out, err = run([idx, "--registry", reg, "--drops", both])
        check(code == 1 and "No drop script: nothing is safe to drop." in out
              and "could not be proven safe" in out,
              "an audit with only an unproven drop exits 1 and writes no "
              "script  <-- pinned defect")
        code, out, err = run([idx, "--registry", reg, "--min-uptime-days",
                              "40"])
        check(code == 0 and "0 drop(s) planned" not in out and
              "held for uptime" in out and "floor 40 days" in out,
              "--min-uptime-days moves the floor")
        code, out, err = run([idx])
        check(code == 3 and "no --registry was given" in err and
              "Nothing was judged." in err and out == "",
              "a refused input exits 3 and prints nothing to stdout")
        code, out, err = run([os.path.join(work, "nope.csv")])
        check(code == 2 and err.startswith("error:"),
              "a file that is not there exits 2")
        code, out, err = run([idx, "--registry", reg, "--out",
                              os.path.join(work, "no", "dir", "x.sql"),
                              "--apply"])
        check(code == 2 and "error:" in err,
              "a script that cannot be written exits 2")
        u16 = put("u16.csv", plain.encode("utf-16"))
        code, out, err = run([u16])
        check(code == 0 and "VERDICT" in out, "a UTF-16 export reads")
        bom = put("bom.sql", u"\ufeffDROP INDEX ix_a ON gis.parcels;"
                  .encode("utf-8"))
        code, out, err = run([put("up.csv", export([xrow("parcels", "ix_a")],
                                                      "720")), "--drops", bom])
        check(code == 0 and "SAFE" in out,
              "a BOM on a DROP list is read past")

        # usage errors
        for argv, label in (
                ([], "no input"),
                ([idx, "--apply"], "--apply with no --out"),
                ([idx, "--min-uptime-days", "x"], "a non-numeric floor"),
                ([idx, "--min-uptime-days", "-3"], "a negative floor"),
                ([idx, "--registry"], "a flag with no value"),
                ([idx, "--nope"], "an unknown flag")):
            code, out, err = run(argv)
            check(code == 64 and err.startswith("error:") and out == "",
                  "usage error exits 64: %s" % label)
        code, out, err = run([idx, "--appl", "--out", out_sql])
        check(code == 64 and not os.path.exists(os.path.join(work, "x")),
              "a unique prefix --appl of --apply is refused by the parser  "
              "<-- pinned defect")
        code, out, err = run([idx, "--regis", reg])
        check(code == 64, "a unique prefix --regis of --registry is refused  "
              "<-- pinned defect")
        code, out, err = run(["--print-query"])
        check(code == 0 and out.count("SELECT") >= 2 and
              "sys.dm_db_index_usage_stats" in out and "SDE_table_registry"
              in out, "--print-query prints both export queries")
        code, out, err = run(["--help"])
        check(code == 0 and "--apply" in out, "--help exits 0")
        real_self_test = globals()["self_test"]
        globals()["self_test"] = lambda: 7
        try:
            code, out, err = run(["--self-test"])
        finally:
            globals()["self_test"] = real_self_test
        check(code == 7, "--self-test exits with the code the self-test "
              "returns  <-- pinned defect")

        # a real child process, with a console that cannot print the name
        accent = put("accent.csv", export([xrow("parcels", u"ix_caf\u00e9")]))
        env = dict(os.environ, PYTHONIOENCODING="ascii")
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), accent],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        cout, cerr = child.communicate()
        check(child.returncode == 0 and b"caf\\xe9" in cout and cerr == b"",
              "a non-ASCII index name prints on an ASCII console instead of "
              "crashing  <-- pinned defect")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    class Stream(object):
        def reconfigure(self, errors):
            self.errors = errors
    fake, bare = Stream(), object()
    _quiet_encoding([fake, bare])
    check(fake.errors == "backslashreplace",
          "a stream that can reconfigure is set to escape what it cannot "
          "print")

    # ---- the query and the source
    for column in INDEX_COLUMNS + (UPTIME_COLUMN,):
        check(re.search(r"(AS %s\b|\bi\.%s\b)" % (column, column),
                        INDEX_QUERY) is not None,
              "the index query writes the column %s" % column)
    for column in REGISTRY_COLUMNS:
        check(re.search(r"\b%s\b" % column, REGISTRY_QUERY) is not None,
              "the registry query writes the column %s" % column)
    check("is_disabled = 0" in INDEX_QUERY,
          "the index query leaves out disabled indexes")
    with open(os.path.abspath(__file__), "rb") as handle:
        source = handle.read()
    check(all(b < 128 for b in bytearray(source)),
          "the source is pure ASCII")
    check(re.search(br"^\s*(import|from)\s+(socket|ssl|urllib|http|ftplib|"
                    br"smtplib|requests|xmlrpc)\b", source, re.M) is None,
          "the source imports no network module")

    # ---- the harness reports a failure, and the module imports quietly
    probe, shown = Recorder(), io.StringIO()
    with contextlib.redirect_stdout(shown):
        probe.check(False, "a false check")
        probe.raises(lambda: None, "a call that raises nothing")
        probe.raises(lambda: 1 // 0, "a call that raises the wrong thing")
        probe.raises(lambda: parse_flag("x", "c"), "a wrong message", "zzz")
        verdict = probe.finish()
    check(verdict == 1 and len(probe.failed) == 4 and
          "4 assertions, 4 failed" in shown.getvalue() and
          "FAILED: a false check" in shown.getvalue(),
          "the harness fails loudly: a false check, no error, the wrong "
          "error and the wrong message all count")
    # Run the source as a module named other than __main__, from memory. Loading
    # it with exec_module would write a __pycache__ next to the script.
    cache = importlib.util.cache_from_source(os.path.abspath(__file__))
    had_cache = os.path.exists(cache)
    twin = {"__name__": "sdeindex_import"}
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown):
        exec(compile(source.decode("ascii"), os.path.abspath(__file__), "exec"),
             twin)
    check(shown.getvalue() == "" and
          twin["DEFAULT_MIN_UPTIME_DAYS"] == DEFAULT_MIN_UPTIME_DAYS,
          "importing the module prints nothing and runs nothing")
    check(had_cache or not os.path.exists(cache),
          "the self-test leaves no bytecode cache behind  <-- pinned defect")

    print("-" * 68)
    return rec.finish()


if __name__ == "__main__":
    sys.exit(main())

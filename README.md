# sdeindex

Plan or audit an index cleanup for an enterprise geodatabase in SQL Server. Refuses a drop list
that removes both copies of an identical pair, and refuses half of a base-table and adds-table
twin. Protects the indexes the geodatabase owns. Holds every "unused" verdict until the usage
counters cover enough uptime.

A parcel layer is versioned. Its table has a nonclustered index on the owner column. The index is
there twice, because two people added it a year apart. A report lists the indexes nobody reads,
and a second report lists the duplicates. Someone merges the two lists into one script.

The unused report names both copies, because neither is read. The duplicate report names one
copy, because it keeps the other. In the merged script both copies drop. The owner search is
slow on Monday, and the server was patched on Sunday, so every counter in the unused report was
nine hours old.

The versioned table adds a second trap. Its edits sit in an adds table. If that table carries a
copy of the attribute index, a script that drops the base copy and leaves the adds copy, or the
reverse, leaves half a pair. Nothing fails at the time. The same report also lists indexes that
the geodatabase created, and nothing in the report says so.

An excerpt of a real run follows. The `...` lines stand for passing assertions left out here. The
count in the last line is the count of the whole run.

```
$ python sdeindex.py --self-test
sdeindex self-test: no server, no network, no credentials
--------------------------------------------------------------------
PASS  a column list reads its order and folds case
...
PASS  a superscript digit is refused, not a crash  <-- pinned defect
...
PASS  of two identical unread indexes the plan keeps one for review and drops the other  <-- pinned defect
...
PASS  a list that drops both of an identical pair is REFUSED on one of them  <-- pinned defect
...
PASS  a base index listed without its adds twin is refused  <-- pinned defect
...
PASS  ALTER TABLE ... DROP CONSTRAINT is read as a removal, as sp_IndexCleanup scripts it  <-- pinned defect
...
PASS  a second index after a WITH clause is refused, not skipped  <-- pinned defect (message names 'several indexes')
...
PASS  a '--' inside a string does not hide the DROP after it  <-- pinned defect
...
PASS  an audit with an unproven drop exits 1  <-- pinned defect
...
PASS  --out without --apply writes nothing  <-- pinned defect
...
PASS  a unique prefix --appl of --apply is refused by the parser  <-- pinned defect
...
--------------------------------------------------------------------
223 assertions, 0 failed
```

## What already exists

Three tools find unused and duplicate indexes well. This tool does not replace them. I read their
source on 2026-10-12. The self-test does not exercise these facts, because this repository
holds none of their code.

- [sp_BlitzIndex](https://github.com/BrentOzarULTD/SQL-Server-First-Responder-Kit) (version 8.34,
  July 2026) diagnoses a whole database. It reads the server start time, so it knows the age of
  the counters.
- [sp_IndexCleanup](https://github.com/erikdarlingdata/DarlingData) (version 2.10, MIT licence)
  by Erik Darling finds unused and duplicate indexes and writes the script. It picks a keeper
  inside each duplicate set. It sets a warning flag under 14 days of uptime. At 7 days or less it
  switches itself to duplicate-only mode. The 14-day floor here is borrowed from it.
- [dbatools](https://github.com/dataplat/dbatools) has `Find-DbaDbUnusedIndex` and
  `Find-DbaDbDuplicateIndex`. The unused check stops when the instance restarted 6 days ago or
  less, unless you pass `-IgnoreUptime`.

I searched the source of sp_IndexCleanup, sp_BlitzIndex and the two dbatools commands for
geodatabase terms. sp_IndexCleanup and the two commands hold none. sp_BlitzIndex holds one hit,
which is about temporal tables. So none of them knows that
`a_7` is the adds table of another table, or that an index named `r7_sde_rowid_uk` belongs to the
geodatabase. And each one judges its own run. A script that someone built from two runs has no
keeper, and no tool above checks it.

This tool adds two things. It knows the geodatabase rules. And its audit mode reads any script of
`DROP INDEX`, `ALTER INDEX ... DISABLE` or `ALTER TABLE ... DROP CONSTRAINT` statements, including
the one sp_IndexCleanup writes, and judges the whole script against the export. A name search of
GitHub for `sdeindex` found no other repository on 2026-10-12.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no database.
The tool reads an index CSV, an optional registry CSV and, in audit mode, one script. It writes
one text file, and only with `--apply`. The self-test also writes a temporary directory, which it
removes, and starts one child Python process.

The self-test prints the same 223 assertions on Windows (Python 3.13.2), on Python 3.9.25 and on
Ubuntu (Python 3.12.3). The outputs match line for line once line endings are normalised. Windows
writes CRLF when you redirect to a file.
`coverage run --branch sdeindex.py --self-test` reports 100 percent of lines and branches.

```
git clone https://github.com/uhsear/sdeindex.git
```

## Usage

Run `--print-query` and read the two queries. Run each in the geodatabase, then save each result
as CSV with a header row. SQL Server 2017 or later is needed, for `STRING_AGG`.

The login needs `VIEW SERVER STATE` on SQL Server 2019 and earlier, or
`VIEW SERVER PERFORMANCE STATE` on SQL Server 2022 and later, because the query reads
[sys.dm_db_index_usage_stats](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-db-index-usage-stats-transact-sql)
and [sys.dm_os_sys_info](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-os-sys-info-transact-sql).
It also needs read access to the registry table.

```
python sdeindex.py --self-test
python sdeindex.py --print-query
python sdeindex.py indexes.csv --registry registry.csv
python sdeindex.py indexes.csv --registry registry.csv --drops naive.sql
python sdeindex.py indexes.csv --registry registry.csv --out drop.sql --apply
python sdeindex.py indexes.csv --registry registry.csv --drops naive.sql --out safe.sql --apply
```

| Flag | Default | What it does |
|---|---|---|
| `indexes` | none | The index export CSV. Required unless `--print-query` or `--self-test`. |
| `--registry` | off | The registry export CSV. Required when a table is named like `a_7` or `d_7`. |
| `--drops` | off | A script to audit instead of planning one. |
| `--min-uptime-days` | `14` | Days of usage counters that an "unused" verdict needs. |
| `--out` | off | Where `--apply` writes the `DROP INDEX` script. |
| `--apply` | off | Write the script to `--out`. Without it nothing is written. |
| `--print-query` | off | Print the two export queries and exit. |
| `--self-test` | off | Run the offline assertions and exit. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--appl` is refused
with exit 64 instead of being read as `--apply`.

`--apply` never overwrites. If `--out` exists, the run exits 2 and the old file stays. The tool
runs nothing against a server. It writes a script, and a person reads it and runs it.

### The example files

These three synthetic files make the two runs below. Save them next to the script and run the
commands to get the same output.

`indexes.csv`:

```
schema_name,table_name,index_name,type_desc,is_unique,is_primary_key,is_unique_constraint,key_columns,included_columns,filter_definition,user_seeks,user_scans,user_lookups,user_updates,uptime_hours
gis,parcels,ix_owner_1,NONCLUSTERED,0,0,0,[owner] ASC,,,0,0,0,0,720
gis,parcels,ix_owner_2,NONCLUSTERED,0,0,0,[owner] ASC,,,0,0,0,0,720
gis,parcels,ix_status,NONCLUSTERED,0,0,0,[status] ASC,,,40,0,0,0,720
gis,parcels,ix_zone,NONCLUSTERED,0,0,0,[zone] ASC,,,0,0,0,120,720
gis,a_7,ix_zone,NONCLUSTERED,0,0,0,[zone] ASC,,,0,0,0,120,720
gis,parcels,pk_parcels,NONCLUSTERED,1,1,0,[objectid] ASC,,,0,0,0,0,720
gis,a_7,a7_idx1,NONCLUSTERED,0,0,0,[sde_state_id] ASC,,,0,0,0,0,720
gis,d_7,d7_idx1,NONCLUSTERED,0,0,0,[sde_deletes_row_id] ASC,,,0,0,0,0,720
sde,SDE_states,states_idx,NONCLUSTERED,0,0,0,[owner] ASC,,,0,0,0,0,720
```

`registry.csv`:

```
registration_id,owner,table_name
7,gis,parcels
```

`naive.sql`:

```
DROP INDEX ix_owner_1 ON gis.parcels;
DROP INDEX ix_owner_2 ON gis.parcels;
DROP INDEX ix_zone ON gis.parcels;
DROP INDEX ix_status ON gis.parcels;
```

### A plan

The export holds two identical owner indexes, one read index, one index with an adds-table twin,
and the geodatabase's own indexes.

```
$ python sdeindex.py indexes.csv --registry registry.csv
uptime: 30.0 days of usage counters, floor 14 days

DROP      [gis].[parcels].[ix_owner_2]  identical to [gis].[parcels].[ix_owner_1], which stays
DROP      [gis].[parcels].[ix_zone] + twin [gis].[a_7].[ix_zone]  no reads in 30.0 days of uptime, 240 write(s) paid for nothing
REVIEW    [gis].[parcels].[ix_owner_1]  every copy is unread, and this is the one the plan keeps. Judge it in a later run, after its duplicates are gone
KEEP      5 unit(s): 4 protected by a rule, 1 read

VERDICT: 2 drop(s) planned, 1 for review, 0 held for uptime
Script not written: 2 unit(s) to drop. Add --out FILE --apply to write it.
```

The two identical owner indexes do not both drop. One drops. The other waits for review, because
every copy is unread, and a later run judges it after the duplicate is gone. The twin pair drops
as one unit, three `DROP INDEX` statements in all.

### An audit

The same export, audited against a merged script that lists both owner indexes, half of the twin
pair, and the read index:

```
$ python sdeindex.py indexes.csv --registry registry.csv --drops naive.sql --out safe.sql --apply
uptime: 30.0 days of usage counters, floor 14 days

REFUSED   [gis].[parcels].[ix_owner_1]  the list drops every copy of an identical set. Keep this one; drop the others first
SAFE      [gis].[parcels].[ix_owner_2]  identical to [gis].[parcels].[ix_owner_1], which stays
REFUSED   [gis].[parcels].[ix_zone]  leaves its twin [gis].[a_7].[ix_zone] behind. The two serve one versioned table, so they go together or not at all
UNPROVEN  [gis].[parcels].[ix_status]  read 40 time(s), and not a copy of an index that stays. These rules cannot prove it redundant

VERDICT: 2 of 4 listed drop(s) refused. Do not run this list as it stands.
Wrote 1 unit(s) to safe.sql. Read it, then run it yourself.
```

The run exits 1. The script `--apply` writes holds the one drop the rules allow:

```
-- sdeindex drop script: 1 index(es) in 1 unit(s), from naive.sql.
-- Read it, then run it yourself. sdeindex runs nothing.
-- A dropped index is gone. Script its CREATE INDEX first if you may want it back.

-- identical to [gis].[parcels].[ix_owner_1], which stays
DROP INDEX [ix_owner_2] ON [gis].[parcels];
```

### What an audit reads

| Statement | Read as |
|---|---|
| `DROP INDEX name ON schema.table` | A removal. `IF EXISTS` and a `WITH (...)` clause are read past. |
| `ALTER INDEX name ON schema.table DISABLE` | A removal. This is the form sp_IndexCleanup writes for an index. |
| `ALTER TABLE schema.table DROP CONSTRAINT name` | The removal of the index behind that constraint. sp_IndexCleanup writes this for a unique constraint. |
| `DROP TABLE` | Refused. It removes every index on the table, and the run exits 3. |
| `ALTER INDEX ... REBUILD` or `REORGANIZE`, other `ALTER TABLE` statements | Not removals. Skipped. |

A statement with several targets, such as `DROP INDEX a ON t, b ON t`, is refused with exit 3. So is
`ALTER INDEX ALL`. Comments are ignored. A `--` or `/*` inside a string or a bracketed name does
not start a comment, and block comments nest as T-SQL nests them. A string, quoted name or
comment that never closes is refused.

## The rules

Each rule below has assertions in the self-test. The label says where.

| Rule | Why | Source |
|---|---|---|
| Never drop every copy of an identical set. | Reads move to the copy that stays. With no copy left, they find nothing. | Self-test, "drops both of an identical pair". |
| Which copy is refused does not depend on list order. | A verdict that follows the order is a coin flip. | Self-test, "does not depend on the order". |
| An adds-table index and its base-table twin go together or not at all. | Half a pair is neither the old state nor the new one. | Self-test, "without its adds twin". |
| A read on either twin keeps the pair. | The counters of the two tables are separate. | Self-test, "a read on the adds twin". |
| Indexes on `a_<id>` and `d_<id>` are judged only when `--registry` proves the table. | Esri names the delta tables `a_<registration_id>` and `d_<registration_id>`, in the schema of the versioned table. | [Esri, geodatabase system tables in SQL Server](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-sql-server/geodatabase-system-tables-sqlserver.html) |
| Every index on a deletes table, and any index on an adds table over `sde_state_id`, `sde_deletes_row_id` or `deleted_at`, is never dropped. | The geodatabase creates them. Esri's Oracle help documents indexes over these columns on its delta tables. The tool does not protect a base-table index by column name. | [Esri, Oracle configuration parameters](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/manage-oracle/configuration-parameters-oracle.html) |
| Anything in the `sde` schema, or on an `SDE_` or `GDB_` table, is never dropped. | These are geodatabase system tables. | The Esri page above lists both prefixes. |
| The ObjectID index is never dropped. | Esri's SDK help: "An attribute index on object id cannot be deleted." The index is unique, and the unique rule protects it. | [Esri, SchemaBuilder.Delete](https://doc.esri.com/en/arcgis-pro/latest/sdk/api-reference/topic40928.html) |
| Primary key, unique constraint, unique, spatial, hash and clustered indexes are never dropped. | Some enforce a rule. The counters never see spatial and memory-optimized indexes. | [Microsoft, sys.dm_db_index_usage_stats](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-db-index-usage-stats-transact-sql) |
| An unread index is held until the counters cover `--min-uptime-days`. | The counters start empty when the database engine starts. A detach or an `AUTO_CLOSE` shutdown also removes the rows. | The Microsoft page above, and [sys.dm_os_sys_info](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-os-sys-info-transact-sql) for `sqlserver_start_time`. |
| With no `uptime_hours` column, no index is called unused. | The export cannot show the age of the counters. | Self-test, "no uptime". |

Microsoft adds a row for an index on its first use, so a blank counter reads as 0.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A plan, or an audit in which every listed drop is `SAFE`. |
| 1 | The audited script holds a removal the rules refuse (`REFUSED`) or cannot prove (`UNPROVEN`). Do not run it as it stands. |
| 2 | A file could not be read, or `--out` could not be written or already exists. |
| 3 | The input was refused. Nothing was judged and nothing was written. |
| 64 | Usage error, including an unknown or abbreviated flag. |

`UNPROVEN` means the index is read and nothing identical stays, so these rules can neither prove
it redundant nor forbid the drop. The audit exits 1, so a gate that reads only the exit code does
not pass it. A person reads each `UNPROVEN` line and decides. The script `--apply` writes holds
`SAFE` drops only.

## Limits

- I did not run the two export queries against SQL Server. No instance was available. I wrote them
  from Microsoft's column documentation for `sys.indexes`, `sys.index_columns` and
  `sys.dm_db_index_usage_stats`. The self-test checks that each query writes each column the
  tool reads. It cannot check that the T-SQL runs. Run it on a test copy first. If it fails,
  that is a bug, and the fix is in `INDEX_QUERY`. The registry query reads `sde.SDE_table_registry`.
  Use `dbo.SDE_table_registry` where the geodatabase system tables sit in the `dbo` schema.
- The delta-table rules rest on Esri's documented names. Esri's SQL Server help names the adds and
  deletes tables. It names no index on them. The tool assumes the Oracle column names hold in SQL
  Server, and protects any adds-table index over one of them. The pattern `r<id>_sde_rowid_uk` is
  a guess at the ObjectID index name. The unique rule protects that index whatever its name.
- The twin rule is a precaution. Esri's documentation does not say its tools create an adds-table
  copy of an attribute index, so the tool pairs one only when it finds one. A twin is an index on
  the adds table with the same type, keys, includes and filter as an index on the base table.
- Only the delta tables of traditional versioning are recognised. Archive class tables
  (`<table>_H`, `GDB_FROM_DATE`, `GDB_TO_DATE`), branch-versioned tables and replica tables get no
  special rule, and the tool treats their indexes as ordinary table indexes.
- A user table named like `a_2019` or `d2020` (letters `a` or `d`, an optional underscore, then
  digits) makes the run need `--registry`. Without it the run exits 3. If such a table has the
  same number as a registry row in the same schema, the tool treats it as that registration's
  adds table.
- The uptime floor covers an engine restart only. A database detach or an `AUTO_CLOSE` shutdown
  clears the rows for one database, and the export cannot show that. The floor of 14 days is
  borrowed from sp_IndexCleanup and is not measured. A report that runs once a quarter reads its
  index four times a year, and no short floor sees it.
- Usage counters belong to one replica. On an Always On readable secondary, reads served there are
  not in the primary's export, so an index can look unread when it is used. Export from every
  replica that serves reads, or treat an unread verdict as unproven.
- Only nonclustered indexes are judged. The export query leaves out disabled and hypothetical
  indexes. Compression, partitioning and fill factor are not compared, so two indexes that differ
  only in those count as identical. The tool can drop a compressed copy and keep an uncompressed one.
- It does not see a hint in a query, a plan guide, or a foreign key that leans on an index. A
  forced index name in application code breaks when the index drops, and the counters do not warn.
- In an audit, every other statement is skipped without a message. That includes `DROP STATISTICS`,
  `TRUNCATE`, and the `CREATE INDEX ... WITH (DROP_EXISTING = ON)` merge statements that
  sp_IndexCleanup also writes. Merge scripts that rewrite a kept index are not audited.
  `ALTER TABLE ... DROP CONSTRAINT` is read as the removal of that constraint's index. A constraint
  that is not an index, such as a foreign key, is refused as not in the index export.
- End every statement in an audited script with a semicolon. Without one, a statement that has no
  semicolon can pull in the words of the next statement and be refused as unreadable.
- The database part of a three-part table name, and any `USE` statement, is ignored. Audit a
  script only against the export of the database it was written for. An index target with no
  schema (`ON parcels`) is matched to the one schema that holds that table in the export, but SQL
  Server resolves it with the connection's default schema. Write the schema on every statement.
- When a base-table index and an identical adds-table twin pair up, and a second identical base
  copy exists, which base copy is the twin follows name order. Listing the other base copy can be
  refused as leaving its twin behind even though the drop is safe. The error is on the safe side.
- The script `--apply` writes always uses `DROP INDEX`, which cannot be undone. Script the
  `CREATE INDEX` statements first if you may want an index back. A line break in an index or table
  name is written out as `\n` in the `--` comment lines. In the `DROP INDEX` line it stays inside
  the brackets, as T-SQL allows. Read the script before you run it.
- The script is written as UTF-8 with no byte order mark. SSMS can read a non-ASCII index name as
  ANSI when you open such a file. The tool reads a BOM and UTF-16 on input.
- The tool reads the whole export into memory. I did not measure a large export. A counter or an
  uptime of more than 19 digits is refused.
- A run that exits 3 writes nothing. A run without `--apply` writes nothing. An audit that exits 1
  still writes the `SAFE` drops when you pass `--apply`. After an exit 2 from `--apply`, delete a
  partial `--out` file before you retry. On Windows, `--out NUL --apply` prints a "Wrote" line
  although nothing is kept. Only `--self-test` writes anything else, a temporary directory that it
  removes.
- It opens no socket, imports no network module and reads no password, token or environment
  variable. The self-test reads its own source and checks the imports. A refusal quotes the value
  it refused, so read a refusal before you paste it into a ticket.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [gdbprune](https://github.com/uhsear/gdbprune) - delete stale leaf versions from a
  traditionally versioned geodatabase, with a plan first. Run it, then a compress, before an index
  cleanup, so the usage counters and the adds tables describe the current data.
- [compressfloor](https://github.com/uhsear/compressfloor) - name what holds the compress floor.
  It reads exported system tables, as this tool reads an exported index list.
- [safe-republish](https://github.com/uhsear/safe-republish) - the same refusal to act on a list
  that nobody checked, applied to a feature class truncate.

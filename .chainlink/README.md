# Chainlink issue snapshot

`issues.json` is the portable, version-controlled export of the project's
Chainlink issues, including comments, labels, priorities, parent links, status,
and lifecycle timestamps.

Refresh it from the repository root with:

```sh
chainlink export --format json --output .chainlink/issues.json
```

The live `issues.db` database and other files in this directory are local
runtime state and intentionally remain ignored. Import the snapshot into a
fresh Chainlink database with:

```sh
chainlink import .chainlink/issues.json
```

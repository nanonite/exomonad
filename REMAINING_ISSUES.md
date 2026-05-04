# Remaining Issues

## 1. ReviewerRole.hs not registered in AllRoles.hs

`AllRoles.hs` has no import of `ReviewerRole` and no entry in `allConfigs`:

```haskell
-- allConfigs currently only contains:
("tl",     mkSomeRoleConfig TLRole.config),
("dev",    mkSomeRoleConfig DevRole.config),
("worker", mkSomeRoleConfig WorkerRole.config),
-- "reviewer" is missing
```

When the Rust server calls the WASM for a `role=reviewer` agent, `lookupRole` returns `Nothing`. The tool restrictions defined in `ReviewerRole.hs` are never enforced — the reviewer agent gets full tool access instead of the restricted set.

**Fix:**

```haskell
import qualified ReviewerRole   -- add to imports

allConfigs :: Map Text SomeRoleConfig
allConfigs = Map.fromList
  [ ("tl",       mkSomeRoleConfig TLRole.config)
  , ("dev",      mkSomeRoleConfig DevRole.config)
  , ("worker",   mkSomeRoleConfig WorkerRole.config)
  , ("reviewer", mkSomeRoleConfig ReviewerRole.config)   -- add this
  ]
```

**File:** `.exo/roles/devswarm/AllRoles.hs` lines 92–98.

-- | Spawn core re-exports for role code.
module ExoMonad.Guest.Records.Spawn
  ( -- * SpawnLeafSubtree
    SpawnLeafSubtreeArgs (..),
    spawnLeafSubtreeCore,
    spawnLeafRender,
    spawnLeafSubtreeDescription,
    spawnLeafSubtreeSchema,

    -- * SpawnWorkers
    SpawnWorkersArgs (..),
    WorkerSpec (..),
    WorkerType (..),
    spawnWorkersCore,
    spawnWorkersDescription,
    spawnWorkersSchema,
    CloseWorkerPaneArgs (..),
    closeWorkerPaneCore,
    closeWorkerPaneDescription,
    closeWorkerPaneSchema,
  )
where

import ExoMonad.Guest.Tools.Spawn

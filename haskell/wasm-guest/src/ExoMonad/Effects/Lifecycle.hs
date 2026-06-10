{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Root lifecycle effects for idle convergence and graceful shutdown.
--
-- Dispatched via the @lifecycle@ namespace.
-- Request and response types are proto-generated from @proto/effects/lifecycle.proto@.
module ExoMonad.Effects.Lifecycle
  ( -- * Effect Types
    LifecycleHasPendingWork,
    LifecycleShutdownServer,

    -- * Re-exported proto types
    module Effects.Lifecycle,
  )
where

import Effects.Lifecycle
import ExoMonad.Effect.Class (Effect (..))

data LifecycleHasPendingWork

instance Effect LifecycleHasPendingWork where
  type Input LifecycleHasPendingWork = HasPendingWorkEffect
  type Output LifecycleHasPendingWork = HasPendingWorkResult
  effectId = "lifecycle.has_pending_work"

data LifecycleShutdownServer

instance Effect LifecycleShutdownServer where
  type Input LifecycleShutdownServer = ServerShutdownEffect
  type Output LifecycleShutdownServer = ServerShutdownResult
  effectId = "lifecycle.shutdown_server"

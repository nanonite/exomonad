{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Root lifecycle effects for idle convergence and graceful shutdown.
--
-- Dispatched via the @lifecycle@ namespace.
-- Request and response types are proto-generated from @proto/effects/lifecycle.proto@.
module ExoMonad.Effects.Lifecycle
  ( -- * Effect Types
    LifecycleShutdownServer,

    -- * Re-exported proto types
    module Effects.Lifecycle,
  )
where

import Effects.Lifecycle
import ExoMonad.Effect.Class (Effect (..))

data LifecycleShutdownServer

instance Effect LifecycleShutdownServer where
  type Input LifecycleShutdownServer = ServerShutdownEffect
  type Output LifecycleShutdownServer = ServerShutdownResult
  effectId = "lifecycle.shutdown_server"

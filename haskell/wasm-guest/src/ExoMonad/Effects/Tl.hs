{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Controller observability effects.
module ExoMonad.Effects.Tl
  ( TlEmitEvent,
    module Effects.Tl,
  )
where

import Effects.Tl
import ExoMonad.Effect.Class (Effect (..))

data TlEmitEvent

instance Effect TlEmitEvent where
  type Input TlEmitEvent = EmitEventRequest
  type Output TlEmitEvent = EmitEventResponse
  effectId = "tl.emit_event"

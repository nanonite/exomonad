{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Effects.Inbox
  ( InboxCheck,

    -- * Proto types
    module Effects.Inbox,
  )
where

import Effects.Inbox
import ExoMonad.Effect.Class (Effect (..))

-- | Check and drain the caller's durable inbox.
data InboxCheck

instance Effect InboxCheck where
  type Input InboxCheck = InboxCheckEffect
  type Output InboxCheck = InboxCheckResult
  effectId = "inbox.check"

{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Effects.Events
  ( EventsNotifyEvent,
    EventsNotifyParent,
    EventsSendMessage,
    EventsSendTmuxMessage,
    EventsSendMailboxMessage,

    -- * Proto types
    module Effects.Events,
  )
where

import Effects.Events
import ExoMonad.Effect.Class (Effect (..))

-- | Notify event effect
data EventsNotifyEvent

instance Effect EventsNotifyEvent where
  type Input EventsNotifyEvent = NotifyEventRequest
  type Output EventsNotifyEvent = NotifyEventResponse
  effectId = "events.notify_event"

-- | Notify parent effect
data EventsNotifyParent

instance Effect EventsNotifyParent where
  type Input EventsNotifyParent = NotifyParentRequest
  type Output EventsNotifyParent = NotifyParentResponse
  effectId = "events.notify_parent"

-- | Send message effect
data EventsSendMessage

instance Effect EventsSendMessage where
  type Input EventsSendMessage = SendMessageRequest
  type Output EventsSendMessage = SendMessageResponse
  effectId = "events.send_message"

-- | Send message through tmux STDIN injection only
data EventsSendTmuxMessage

instance Effect EventsSendTmuxMessage where
  type Input EventsSendTmuxMessage = SendTmuxMessageRequest
  type Output EventsSendTmuxMessage = SendTmuxMessageResponse
  effectId = "events.send_tmux_message"

-- | Send message through Claude Teams inbox only
data EventsSendMailboxMessage

instance Effect EventsSendMailboxMessage where
  type Input EventsSendMailboxMessage = SendMailboxMessageRequest
  type Output EventsSendMailboxMessage = SendMailboxMessageResponse
  effectId = "events.send_mailbox_message"

-- | Events core re-exports for role code.
module ExoMonad.Guest.Records.Events
  ( -- * Message tools (MCPTool instances stay in SDK)
    SendTmuxMessage,
    SendMailboxMessage,
    SendMessageArgs (..),

    -- * NotifyParent (core + shared schema, no MCPTool instance)
    NotifyParent,
    NotifyParentArgs (..),
    notifyParentCore,
    notifyParentDescription,
    notifyParentSchema,
    NotifyStatus (..),
    TaskReport (..),
  )
where

import ExoMonad.Guest.Tools.Events

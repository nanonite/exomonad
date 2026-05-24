{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}
{-# LANGUAGE TypeOperators #-}

-- | E2E test root role: minimal tools with PII rewriting hooks.
-- The Gemini root agent uses httpDevHooks for BeforeModel/AfterModel rewriting.
-- Only split message tools are needed (Gemini writes files via its native tools).
module RootRole (config, Tools) where

import ExoMonad
import ExoMonad.Guest.Types (allowStopResponse, BeforeModelOutput (..), AfterModelOutput (..))
import ExoMonad.Types (HookConfig (..), defaultSessionStartHook)
import HttpDevHooks (httpDevHooks)

data Tools mode = Tools
  { sendTmuxMessage :: mode :- SendTmuxMessage,
    sendMailboxMessage :: mode :- SendMailboxMessage
  }
  deriving (Generic)

config :: RoleConfig (Tools AsHandler)
config =
  RoleConfig
    { roleName = "root",
      tools = Tools
        { sendTmuxMessage = mkHandler @SendTmuxMessage,
            sendMailboxMessage = mkHandler @SendMailboxMessage
        },
      hooks = httpDevHooks,
      eventHandlers = defaultEventHandlers
    }

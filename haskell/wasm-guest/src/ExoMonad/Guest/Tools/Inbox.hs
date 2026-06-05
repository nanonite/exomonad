{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Inbox
  ( CheckInbox (..),
    CheckInboxArgs (..),
    checkInboxDescription,
    checkInboxSchema,
    checkInboxCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import ExoMonad.Effects.Inbox qualified as Inbox
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

-- | No-argument inbox check request. The host resolves the caller identity.
data CheckInboxArgs = CheckInboxArgs
  deriving (Generic, Show)

instance FromJSON CheckInboxArgs where
  parseJSON = withObject "CheckInboxArgs" $ \_ -> pure CheckInboxArgs

instance ToJSON CheckInboxArgs where
  toJSON CheckInboxArgs = object []

checkInboxDescription :: Text
checkInboxDescription = "Check and mark as read the durable inbox messages addressed to the current agent. Returns message sender, content, summary, and creation time."

checkInboxSchema :: Aeson.Object
checkInboxSchema = genericToolSchemaWith @CheckInboxArgs []

checkInboxCore :: CheckInboxArgs -> Eff Effects (Either Text Value)
checkInboxCore _args = do
  result <- suspendEffect @Inbox.InboxCheck Inbox.InboxCheckEffect {}
  pure $ case result of
    Left err -> Left ("inbox.check failed: " <> T.pack (show err))
    Right resp ->
      let messages = V.toList (Inbox.inboxCheckResultMessages resp)
       in Right $
            object
              [ "count" .= length messages,
                "messages" .= map inboxMessageValue messages
              ]

inboxMessageValue :: Inbox.InboxMessage -> Value
inboxMessageValue message =
  object
    [ "from_agent" .= strictText (Inbox.inboxMessageFromAgent message),
      "content" .= strictText (Inbox.inboxMessageContent message),
      "summary" .= strictText (Inbox.inboxMessageSummary message),
      "created_at" .= Inbox.inboxMessageCreatedAt message
    ]

strictText :: TL.Text -> Text
strictText = TL.toStrict

data CheckInbox = CheckInbox

instance MCPTool CheckInbox where
  type ToolArgs CheckInbox = CheckInboxArgs
  toolName = "check_inbox"
  toolDescription = checkInboxDescription
  toolSchema = checkInboxSchema
  toolHandlerEff args = do
    result <- checkInboxCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

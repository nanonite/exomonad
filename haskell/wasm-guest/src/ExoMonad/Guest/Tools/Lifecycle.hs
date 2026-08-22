{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Lifecycle
  ( ShutdownServer (..),
    ShutdownServerArgs (..),
    shutdownServerDescription,
    shutdownServerSchema,
    shutdownServerCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import ExoMonad.Effects.Lifecycle qualified as Lifecycle
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data ShutdownServerArgs = ShutdownServerArgs
  deriving (Generic, Show)

instance FromJSON ShutdownServerArgs where
  parseJSON = withObject "ShutdownServerArgs" $ \_ -> pure ShutdownServerArgs

instance ToJSON ShutdownServerArgs where
  toJSON ShutdownServerArgs = object []

shutdownServerDescription :: Text
shutdownServerDescription = "Gracefully shut down the shared ExoMonad server after server-side verification finds no alive non-root agents."

shutdownServerSchema :: Aeson.Object
shutdownServerSchema = genericToolSchemaWith @ShutdownServerArgs []

shutdownServerCore :: ShutdownServerArgs -> Eff Effects (Either Text Value)
shutdownServerCore _args = do
  result <- suspendEffect @Lifecycle.LifecycleShutdownServer Lifecycle.ServerShutdownEffect {}
  pure $ case result of
    Left err -> Left ("lifecycle.shutdown_server failed: " <> T.pack (show err))
    Right resp ->
      Right $
        object
          [ "success" .= Lifecycle.serverShutdownResultSuccess resp,
            "error" .= strictText (Lifecycle.serverShutdownResultError resp),
            "message" .= strictText (Lifecycle.serverShutdownResultMessage resp)
          ]

strictText :: TL.Text -> Text
strictText = TL.toStrict

-- This is an operator/server path, not an agent-role tool. The source-derived
-- role coverage check recognizes this annotation rather than a name allowlist.
-- exomonad-role: operator-only
data ShutdownServer = ShutdownServer

instance MCPTool ShutdownServer where
  type ToolArgs ShutdownServer = ShutdownServerArgs
  toolName = "shutdown_server"
  toolDescription = shutdownServerDescription
  toolSchema = shutdownServerSchema
  toolHandlerEff args = do
    result <- shutdownServerCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

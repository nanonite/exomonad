{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.DiscardWorkerOutput
  ( DiscardWorkerOutput (..),
    DiscardWorkerOutputArgs (..),
    discardWorkerOutputDescription,
    discardWorkerOutputSchema,
    discardWorkerOutputCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.:), (.=))
import Data.Aeson qualified as Aeson
import Data.Map qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Data.Word (Word64)
import Effects.Process qualified as Proc
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data DiscardWorkerOutputArgs = DiscardWorkerOutputArgs
  { dwoReason :: Text
  }
  deriving (Generic, Show)

instance FromJSON DiscardWorkerOutputArgs where
  parseJSON = withObject "DiscardWorkerOutputArgs" $ \v ->
    DiscardWorkerOutputArgs <$> v .: "reason"

instance ToJSON DiscardWorkerOutputArgs where
  toJSON args = object ["reason" .= dwoReason args]

discardWorkerOutputDescription :: Text
discardWorkerOutputDescription =
  "TL-only escape hatch that discards unstaged worker output in the current worktree. Refuses staged changes."

discardWorkerOutputSchema :: Aeson.Object
discardWorkerOutputSchema =
  genericToolSchemaWith @DiscardWorkerOutputArgs
    [("reason", "Why the worker output is safe to discard")]

discardWorkerOutputCore :: DiscardWorkerOutputArgs -> Eff Effects (Either Text Aeson.Value)
discardWorkerOutputCore args = do
  staged <- runGit ["diff", "--cached", "--name-only"]
  case staged of
    Left err -> pure (Left err)
    Right files
      | not (T.null (T.strip files)) ->
          pure $ Left $ "Refusing to discard worker output because staged changes exist:\n" <> files
    Right _ -> do
      restored <- runGit ["restore", "."]
      cleaned <- runGit ["clean", "-fd"]
      case (restored, cleaned) of
        (Right _, Right cleanOutput) ->
          pure $ Right $ object ["success" .= True, "reason" .= dwoReason args, "cleaned" .= cleanOutput]
        (Left err, _) -> pure (Left err)
        (_, Left err) -> pure (Left err)

runGit :: [Text] -> Eff Effects (Either Text Text)
runGit args = do
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "git",
            Proc.runRequestArgs = V.fromList (TL.fromStrict <$> args),
            Proc.runRequestWorkingDir = ".",
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = 30000 :: Word64
          }
      )
  case result of
    Left err -> pure $ Left (T.pack (show err))
    Right resp
      | Proc.runResponseExitCode resp == 0 -> pure $ Right (TL.toStrict (Proc.runResponseStdout resp))
      | otherwise -> pure $ Left (TL.toStrict (Proc.runResponseStderr resp))

data DiscardWorkerOutput

instance MCPTool DiscardWorkerOutput where
  type ToolArgs DiscardWorkerOutput = DiscardWorkerOutputArgs
  toolName = "discard_worker_output"
  toolDescription = discardWorkerOutputDescription
  toolSchema = discardWorkerOutputSchema
  toolHandlerEff args = do
    result <- discardWorkerOutputCore args
    case result of
      Left err -> pure $ errorResult err
      Right value -> pure $ successResult value

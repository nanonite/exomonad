{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.Chainlink
  ( -- * Issue Create
    ChainlinkIssueCreate (..),
    chainlinkIssueCreateCore,
    chainlinkIssueCreateDescription,
    chainlinkIssueCreateSchema,
    ChainlinkIssueCreateArgs (..),
    ChainlinkIssueCreateOutput (..),
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:), (.:?), (.=))
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

chainlinkTimeoutMs :: Word64
chainlinkTimeoutMs = 30000

--------------------------------------------------------------------------------
-- Issue Create
--------------------------------------------------------------------------------

data ChainlinkIssueCreateArgs = ChainlinkIssueCreateArgs
  { cicaTitle :: Text,
    cicaDescription :: Maybe Text,
    cicaPriority :: Maybe Text,
    cicaLabels :: Maybe [Text]
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueCreateArgs where
  parseJSON = withObject "ChainlinkIssueCreateArgs" $ \v ->
    ChainlinkIssueCreateArgs
      <$> v .: "title"
      <*> v .:? "description"
      <*> v .:? "priority"
      <*> v .:? "labels"

instance ToJSON ChainlinkIssueCreateArgs where
  toJSON args =
    object
      [ "title" .= cicaTitle args,
        "description" .= cicaDescription args,
        "priority" .= cicaPriority args,
        "labels" .= cicaLabels args
      ]

data ChainlinkIssueCreateOutput = ChainlinkIssueCreateOutput
  { cicoIssueId :: Int
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueCreateOutput

instance ToJSON ChainlinkIssueCreateOutput

chainlinkIssueCreateDescription :: Text
chainlinkIssueCreateDescription =
  "Create a new chainlink issue. Returns the created issue ID. Use this to track work items, bugs, features, and tasks in the project's chainlink tracker."

chainlinkIssueCreateSchema :: Aeson.Object
chainlinkIssueCreateSchema =
  genericToolSchemaWith @ChainlinkIssueCreateArgs
    [ ("title", "Issue title — should be a concise, changelog-ready description of the work"),
      ("description", "Optional detailed description of the issue"),
      ("priority", "Optional priority: low, medium, high, critical (default: medium)"),
      ("labels", "Optional list of labels to apply (e.g. bug, feature, enhancement)")
    ]

chainlinkIssueCreateCore :: ChainlinkIssueCreateArgs -> Eff Effects (Either Text ChainlinkIssueCreateOutput)
chainlinkIssueCreateCore args = do
  let cmdArgs = buildCreateArgs args
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "chainlink",
            Proc.runRequestArgs = V.fromList (TL.pack <$> cmdArgs),
            Proc.runRequestWorkingDir = ".",
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = chainlinkTimeoutMs
          }
      )
  case result of
    Left err -> pure $ Left ("chainlink create failed: " <> T.pack (show err))
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case parseIssueId (TL.toStrict (Proc.runResponseStdout resp)) of
            Just issueId ->
              pure $ Right (ChainlinkIssueCreateOutput issueId)
            Nothing ->
              pure $ Left ("could not parse issue ID from output: " <> TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink create failed (exit "
                <> T.pack (show (Proc.runResponseExitCode resp))
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

buildCreateArgs :: ChainlinkIssueCreateArgs -> [String]
buildCreateArgs args =
  ["create", T.unpack (cicaTitle args), "-q"]
    ++ case cicaPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case cicaLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []

parseIssueId :: Text -> Maybe Int
parseIssueId output =
  case T.strip output of
    t
      | T.all isDigit t -> Just (read (T.unpack t))
      | otherwise -> Nothing
  where
    isDigit c = c >= '0' && c <= '9'

data ChainlinkIssueCreate

instance MCPTool ChainlinkIssueCreate where
  type ToolArgs ChainlinkIssueCreate = ChainlinkIssueCreateArgs
  toolName = "chainlink_issue_create"
  toolDescription = chainlinkIssueCreateDescription
  toolSchema = chainlinkIssueCreateSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCreateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

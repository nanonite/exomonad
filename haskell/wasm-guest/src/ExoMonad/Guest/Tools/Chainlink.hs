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

    -- * Issue Show
    ChainlinkIssueShow (..),
    chainlinkIssueShowCore,
    chainlinkIssueShowDescription,
    chainlinkIssueShowSchema,
    ChainlinkIssueShowArgs (..),

    -- * Issue Comment
    ChainlinkIssueComment (..),
    chainlinkIssueCommentCore,
    chainlinkIssueCommentDescription,
    chainlinkIssueCommentSchema,
    ChainlinkIssueCommentArgs (..),

    -- * Subissue Create
    ChainlinkSubissueCreate (..),
    chainlinkSubissueCreateCore,
    chainlinkSubissueCreateDescription,
    chainlinkSubissueCreateSchema,
    ChainlinkSubissueCreateArgs (..),

    -- * Session Work
    ChainlinkSessionWork (..),
    chainlinkSessionWorkCore,
    chainlinkSessionWorkDescription,
    chainlinkSessionWorkSchema,
    ChainlinkSessionWorkArgs (..),

    -- * Session End
    ChainlinkSessionEnd (..),
    chainlinkSessionEndCore,
    chainlinkSessionEndDescription,
    chainlinkSessionEndSchema,
    ChainlinkSessionEndArgs (..),

    -- * Issue Close
    ChainlinkIssueClose (..),
    chainlinkIssueCloseCore,
    chainlinkIssueCloseDescription,
    chainlinkIssueCloseSchema,
    ChainlinkIssueCloseArgs (..),
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
import ExoMonad.Chainlink.Pure
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

chainlinkTimeoutMs :: Word64
chainlinkTimeoutMs = 30000

runChainlink :: [String] -> Eff Effects (Either Text Proc.RunResponse)
runChainlink cmdArgs = do
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
    Left err -> pure $ Left (T.pack (show err))
    Right resp -> pure $ Right resp

exitCodeToText :: Int -> Text
exitCodeToText = T.pack . show

--------------------------------------------------------------------------------
-- Issue Create
--------------------------------------------------------------------------------

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
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink create failed: " <> err)
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
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

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

--------------------------------------------------------------------------------
-- Issue Show
--------------------------------------------------------------------------------

data ChainlinkIssueShowArgs = ChainlinkIssueShowArgs
  { cisaIssueId :: Int
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueShowArgs where
  parseJSON = withObject "ChainlinkIssueShowArgs" $ \v ->
    ChainlinkIssueShowArgs <$> v .: "issue_id"

instance ToJSON ChainlinkIssueShowArgs where
  toJSON args =
    object
      [ "issue_id" .= cisaIssueId args
      ]

chainlinkIssueShowDescription :: Text
chainlinkIssueShowDescription =
  "Show details of a chainlink issue by ID. Returns the issue title, status, priority, and labels. Use this to inspect a task before working on it."

chainlinkIssueShowSchema :: Aeson.Object
chainlinkIssueShowSchema =
  genericToolSchemaWith @ChainlinkIssueShowArgs
    [ ("issue_id", "The numeric ID of the issue to view")
    ]

chainlinkIssueShowCore :: ChainlinkIssueShowArgs -> Eff Effects (Either Text ChainlinkIssueShowOutput)
chainlinkIssueShowCore args = do
  let cmdArgs = buildShowArgs (cisaIssueId args)
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink issue show failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case Aeson.eitherDecodeStrict (TL.toStrict (Proc.runResponseStdout resp)) of
            Right output -> pure $ Right output
            Left parseErr ->
              pure $ Left ("could not parse issue show output: " <> T.pack parseErr)
      | otherwise ->
          pure $
            Left $
              "chainlink issue show failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueShow

instance MCPTool ChainlinkIssueShow where
  type ToolArgs ChainlinkIssueShow = ChainlinkIssueShowArgs
  toolName = "chainlink_issue_show"
  toolDescription = chainlinkIssueShowDescription
  toolSchema = chainlinkIssueShowSchema
  toolHandlerEff args = do
    result <- chainlinkIssueShowCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Issue Comment
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueCommentArgs where
  parseJSON = withObject "ChainlinkIssueCommentArgs" $ \v ->
    ChainlinkIssueCommentArgs
      <$> v .: "issue_id"
      <*> v .: "message"

instance ToJSON ChainlinkIssueCommentArgs where
  toJSON args =
    object
      [ "issue_id" .= cicIssueId args,
        "message" .= cicMessage args
      ]

chainlinkIssueCommentDescription :: Text
chainlinkIssueCommentDescription =
  "Add a comment to a chainlink issue. Use this to report progress, ask questions, or provide updates on a task."

chainlinkIssueCommentSchema :: Aeson.Object
chainlinkIssueCommentSchema =
  genericToolSchemaWith @ChainlinkIssueCommentArgs
    [ ("issue_id", "The numeric ID of the issue to comment on"),
      ("message", "The comment text to add")
    ]

chainlinkIssueCommentCore :: ChainlinkIssueCommentArgs -> Eff Effects (Either Text ChainlinkIssueCommentOutput)
chainlinkIssueCommentCore args = do
  let cmdArgs = buildCommentArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink comment failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right (ChainlinkIssueCommentOutput True)
      | otherwise ->
          pure $
            Left $
              "chainlink comment failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueComment

instance MCPTool ChainlinkIssueComment where
  type ToolArgs ChainlinkIssueComment = ChainlinkIssueCommentArgs
  toolName = "chainlink_issue_comment"
  toolDescription = chainlinkIssueCommentDescription
  toolSchema = chainlinkIssueCommentSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCommentCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Subissue Create
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSubissueCreateArgs where
  parseJSON = withObject "ChainlinkSubissueCreateArgs" $ \v ->
    ChainlinkSubissueCreateArgs
      <$> v .: "parent_id"
      <*> v .: "title"
      <*> v .:? "priority"
      <*> v .:? "labels"

instance ToJSON ChainlinkSubissueCreateArgs where
  toJSON args =
    object
      [ "parent_id" .= cscParentId args,
        "title" .= cscTitle args,
        "priority" .= cscPriority args,
        "labels" .= cscLabels args
      ]

chainlinkSubissueCreateDescription :: Text
chainlinkSubissueCreateDescription =
  "Create a new sub-issue under a parent chainlink issue. Returns the created sub-issue ID. Use this to break down tasks into smaller work items."

chainlinkSubissueCreateSchema :: Aeson.Object
chainlinkSubissueCreateSchema =
  genericToolSchemaWith @ChainlinkSubissueCreateArgs
    [ ("parent_id", "The numeric ID of the parent issue"),
      ("title", "Sub-issue title — should be a concise, changelog-ready description"),
      ("priority", "Optional priority: low, medium, high, critical (default: medium)"),
      ("labels", "Optional list of labels to apply (e.g. bug, feature, enhancement)")
    ]

chainlinkSubissueCreateCore :: ChainlinkSubissueCreateArgs -> Eff Effects (Either Text ChainlinkIssueCreateOutput)
chainlinkSubissueCreateCore args = do
  let cmdArgs = buildSubissueArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink subissue create failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          case parseIssueId (TL.toStrict (Proc.runResponseStdout resp)) of
            Just issueId ->
              pure $ Right (ChainlinkIssueCreateOutput issueId)
            Nothing ->
              pure $ Left ("could not parse sub-issue ID from output: " <> TL.toStrict (Proc.runResponseStdout resp))
      | otherwise ->
          pure $
            Left $
              "chainlink subissue create failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSubissueCreate

instance MCPTool ChainlinkSubissueCreate where
  type ToolArgs ChainlinkSubissueCreate = ChainlinkSubissueCreateArgs
  toolName = "chainlink_subissue_create"
  toolDescription = chainlinkSubissueCreateDescription
  toolSchema = chainlinkSubissueCreateSchema
  toolHandlerEff args = do
    result <- chainlinkSubissueCreateCore args
    case result of
      Left err -> pure $ errorResult err
      Right output -> pure $ successResult (Aeson.toJSON output)

--------------------------------------------------------------------------------
-- Session Work
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSessionWorkArgs where
  parseJSON = withObject "ChainlinkSessionWorkArgs" $ \v ->
    ChainlinkSessionWorkArgs <$> v .: "issue_id"

instance ToJSON ChainlinkSessionWorkArgs where
  toJSON args =
    object
      [ "issue_id" .= cswIssueId args
      ]

chainlinkSessionWorkDescription :: Text
chainlinkSessionWorkDescription =
  "Mark a chainlink issue as the current active work item. Use this to indicate which issue you are actively working on."

chainlinkSessionWorkSchema :: Aeson.Object
chainlinkSessionWorkSchema =
  genericToolSchemaWith @ChainlinkSessionWorkArgs
    [ ("issue_id", "The numeric ID of the issue to mark as active work")
    ]

chainlinkSessionWorkCore :: ChainlinkSessionWorkArgs -> Eff Effects (Either Text ())
chainlinkSessionWorkCore args = do
  let cmdArgs = buildSessionWorkArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink session work failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink session work failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionWork

instance MCPTool ChainlinkSessionWork where
  type ToolArgs ChainlinkSessionWork = ChainlinkSessionWorkArgs
  toolName = "chainlink_session_work"
  toolDescription = chainlinkSessionWorkDescription
  toolSchema = chainlinkSessionWorkSchema
  toolHandlerEff args = do
    result <- chainlinkSessionWorkCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Session End
--------------------------------------------------------------------------------

instance FromJSON ChainlinkSessionEndArgs where
  parseJSON = withObject "ChainlinkSessionEndArgs" $ \v ->
    ChainlinkSessionEndArgs <$> v .:? "notes"

instance ToJSON ChainlinkSessionEndArgs where
  toJSON args =
    object
      [ "notes" .= cseNotes args
      ]

chainlinkSessionEndDescription :: Text
chainlinkSessionEndDescription =
  "End the current chainlink session with optional notes. Use this when you finish working on a task to record what was done."

chainlinkSessionEndSchema :: Aeson.Object
chainlinkSessionEndSchema =
  genericToolSchemaWith @ChainlinkSessionEndArgs
    [ ("notes", "Optional handoff notes describing what was accomplished during the session")
    ]

chainlinkSessionEndCore :: ChainlinkSessionEndArgs -> Eff Effects (Either Text ())
chainlinkSessionEndCore args = do
  let cmdArgs = buildSessionEndArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink session end failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink session end failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkSessionEnd

instance MCPTool ChainlinkSessionEnd where
  type ToolArgs ChainlinkSessionEnd = ChainlinkSessionEndArgs
  toolName = "chainlink_session_end"
  toolDescription = chainlinkSessionEndDescription
  toolSchema = chainlinkSessionEndSchema
  toolHandlerEff args = do
    result <- chainlinkSessionEndCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

--------------------------------------------------------------------------------
-- Issue Close
--------------------------------------------------------------------------------

instance FromJSON ChainlinkIssueCloseArgs where
  parseJSON = withObject "ChainlinkIssueCloseArgs" $ \v ->
    ChainlinkIssueCloseArgs <$> v .: "issue_id"

instance ToJSON ChainlinkIssueCloseArgs where
  toJSON args =
    object
      [ "issue_id" .= cisIssueId args
      ]

chainlinkIssueCloseDescription :: Text
chainlinkIssueCloseDescription =
  "Close a chainlink issue. Use this when a task is completed. The issue will be marked as closed and added to the changelog."

chainlinkIssueCloseSchema :: Aeson.Object
chainlinkIssueCloseSchema =
  genericToolSchemaWith @ChainlinkIssueCloseArgs
    [ ("issue_id", "The numeric ID of the issue to close")
    ]

chainlinkIssueCloseCore :: ChainlinkIssueCloseArgs -> Eff Effects (Either Text ())
chainlinkIssueCloseCore args = do
  let cmdArgs = buildCloseArgs args
  result <- runChainlink cmdArgs
  case result of
    Left err -> pure $ Left ("chainlink close failed: " <> err)
    Right resp
      | Proc.runResponseExitCode resp == 0 ->
          pure $ Right ()
      | otherwise ->
          pure $
            Left $
              "chainlink close failed (exit "
                <> exitCodeToText (Proc.runResponseExitCode resp)
                <> "): "
                <> TL.toStrict (Proc.runResponseStderr resp)

data ChainlinkIssueClose

instance MCPTool ChainlinkIssueClose where
  type ToolArgs ChainlinkIssueClose = ChainlinkIssueCloseArgs
  toolName = "chainlink_issue_close"
  toolDescription = chainlinkIssueCloseDescription
  toolSchema = chainlinkIssueCloseSchema
  toolHandlerEff args = do
    result <- chainlinkIssueCloseCore args
    case result of
      Left err -> pure $ errorResult err
      Right _ -> pure $ successResult (object ["success" .= True])

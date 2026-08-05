{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | Append-only session-memory tools. All persistence and brief assembly stay
-- on the Rust host; the guest only validates arguments and yields effects.
module ExoMonad.Guest.Tools.Memory
  ( MemoryAppend (..),
    MemoryAppendArgs (..),
    MemoryList (..),
    MemoryListArgs (..),
    ContinuationBrief (..),
    ContinuationBriefArgs (..),
    memoryAppendDescription,
    memoryAppendSchema,
    memoryListDescription,
    memoryListSchema,
    continuationBriefDescription,
    continuationBriefSchema,
    memoryAppendCore,
    memoryListCore,
    continuationBriefCore,
    validMemoryKinds,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Int (Int32)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import ExoMonad.Effects.Memory qualified as Memory
import ExoMonad.Effects.Memory qualified as Proto
import ExoMonad.Guest.Tool.Class (MCPCallOutput, MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (JsonSchema (..), genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)
import Proto3.Suite.Types qualified as PBT

validMemoryKinds :: [Text]
validMemoryKinds =
  [ "unspecified",
    "original_plan",
    "wave_plan",
    "spawned_child",
    "child_handoff",
    "blocker",
    "decision",
    "review_feedback",
    "fix_direction",
    "merge_result",
    "ci_result",
    "next_action",
    "human_clarification",
    "session_summary"
  ]

data MemoryKindArg
  = KindUnspecified
  | KindOriginalPlan
  | KindWavePlan
  | KindSpawnedChild
  | KindChildHandoff
  | KindBlocker
  | KindDecision
  | KindReviewFeedback
  | KindFixDirection
  | KindMergeResult
  | KindCiResult
  | KindNextAction
  | KindHumanClarification
  | KindSessionSummary
  deriving (Eq, Generic, Show)

instance JsonSchema MemoryKindArg where
  toSchema = Aeson.object ["type" .= ("string" :: Text), "enum" .= validMemoryKinds]

instance FromJSON MemoryKindArg where
  parseJSON = Aeson.withText "MemoryKind" $ \value ->
    case value of
      "unspecified" -> pure KindUnspecified
      "original_plan" -> pure KindOriginalPlan
      "wave_plan" -> pure KindWavePlan
      "spawned_child" -> pure KindSpawnedChild
      "child_handoff" -> pure KindChildHandoff
      "blocker" -> pure KindBlocker
      "decision" -> pure KindDecision
      "review_feedback" -> pure KindReviewFeedback
      "fix_direction" -> pure KindFixDirection
      "merge_result" -> pure KindMergeResult
      "ci_result" -> pure KindCiResult
      "next_action" -> pure KindNextAction
      "human_clarification" -> pure KindHumanClarification
      "session_summary" -> pure KindSessionSummary
      other -> fail $ "Unknown memory kind: " <> T.unpack other

instance ToJSON MemoryKindArg where
  toJSON = Aeson.String . kindText

kindText :: MemoryKindArg -> Text
kindText kind =
  case kind of
    KindUnspecified -> "unspecified"
    KindOriginalPlan -> "original_plan"
    KindWavePlan -> "wave_plan"
    KindSpawnedChild -> "spawned_child"
    KindChildHandoff -> "child_handoff"
    KindBlocker -> "blocker"
    KindDecision -> "decision"
    KindReviewFeedback -> "review_feedback"
    KindFixDirection -> "fix_direction"
    KindMergeResult -> "merge_result"
    KindCiResult -> "ci_result"
    KindNextAction -> "next_action"
    KindHumanClarification -> "human_clarification"
    KindSessionSummary -> "session_summary"

protoKind :: MemoryKindArg -> Int32
protoKind kind =
  case kind of
    KindUnspecified -> 0
    KindOriginalPlan -> 1
    KindWavePlan -> 2
    KindSpawnedChild -> 3
    KindChildHandoff -> 4
    KindBlocker -> 5
    KindDecision -> 6
    KindReviewFeedback -> 7
    KindFixDirection -> 8
    KindMergeResult -> 9
    KindCiResult -> 10
    KindNextAction -> 11
    KindHumanClarification -> 12
    KindSessionSummary -> 13

data MemoryAppendArgs = MemoryAppendArgs
  { memoryAppendArgsKind :: MemoryKindArg,
    memoryAppendArgsSummary :: Text,
    memoryAppendArgsDetail :: Maybe Text,
    memoryAppendArgsImportance :: Maybe Int,
    memoryAppendArgsIssueId :: Maybe Int
  }
  deriving (Generic, Show)

instance FromJSON MemoryAppendArgs where
  parseJSON = withObject "MemoryAppendArgs" $ \value ->
    MemoryAppendArgs
      <$> value .: "kind"
      <*> value .: "summary"
      <*> value .:? "detail"
      <*> value .:? "importance"
      <*> value .:? "issue_id"

instance ToJSON MemoryAppendArgs where
  toJSON args =
    object
      [ "kind" .= memoryAppendArgsKind args,
        "summary" .= memoryAppendArgsSummary args,
        "detail" .= memoryAppendArgsDetail args,
        "importance" .= memoryAppendArgsImportance args,
        "issue_id" .= memoryAppendArgsIssueId args
      ]

memoryAppendDescription :: Text
memoryAppendDescription =
  "Append a durable session-memory record. Valid kind values: "
    <> T.intercalate ", " validMemoryKinds
    <> ". The ledger is append-only: records cannot be updated or deleted."

memoryAppendSchema :: Aeson.Object
memoryAppendSchema =
  genericToolSchemaWith @MemoryAppendArgs
    [ ("kind", "Semantic memory category; unrecognized values are rejected."),
      ("summary", "A concise durable fact, at most 200 characters."),
      ("detail", "Optional detail, at most 4096 bytes."),
      ("importance", "Optional priority from 0 to 100; defaults to 50."),
      ("issue_id", "Optional Chainlink issue number.")
    ]

memoryAppendCore :: MemoryAppendArgs -> Eff Effects (Either Text Value)
memoryAppendCore args = do
  result <-
    suspendEffect @Memory.MemoryAppend
      ( Memory.MemoryAppendRequest
          { Proto.memoryAppendRequestRunId = "",
            Proto.memoryAppendRequestAgentId = "",
            Proto.memoryAppendRequestBirthBranch = "",
            Proto.memoryAppendRequestIssueId = maybe 0 fromIntegral (memoryAppendArgsIssueId args),
            Proto.memoryAppendRequestKind = protoKind (memoryAppendArgsKind args),
            Proto.memoryAppendRequestImportance = maybe 0 fromIntegral (memoryAppendArgsImportance args),
            Proto.memoryAppendRequestSummary = TL.fromStrict (memoryAppendArgsSummary args),
            Proto.memoryAppendRequestDetail = maybe "" TL.fromStrict (memoryAppendArgsDetail args),
            Proto.memoryAppendRequestSupersedesId = 0,
            Proto.memoryAppendRequestMetadataJson = ""
          }
      )
  pure $ case result of
    Left err -> Left ("memory.append failed: " <> T.pack (show err))
    Right response -> Right $ object ["id" .= Proto.memoryAppendResponseId response]

data MemoryListArgs = MemoryListArgs
  { memoryListArgsKind :: Maybe MemoryKindArg,
    memoryListArgsIssueId :: Maybe Int,
    memoryListArgsMinImportance :: Maybe Int,
    memoryListArgsLimit :: Maybe Int
  }
  deriving (Generic, Show)

instance FromJSON MemoryListArgs where
  parseJSON = withObject "MemoryListArgs" $ \value ->
    MemoryListArgs
      <$> value .:? "kind"
      <*> value .:? "issue_id"
      <*> value .:? "min_importance"
      <*> value .:? "limit"

instance ToJSON MemoryListArgs where
  toJSON args =
    object
      [ "kind" .= memoryListArgsKind args,
        "issue_id" .= memoryListArgsIssueId args,
        "min_importance" .= memoryListArgsMinImportance args,
        "limit" .= memoryListArgsLimit args
      ]

memoryListDescription :: Text
memoryListDescription = "List durable session-memory records for the current run with optional semantic filters."

memoryListSchema :: Aeson.Object
memoryListSchema = genericToolSchemaWith @MemoryListArgs []

memoryListCore :: MemoryListArgs -> Eff Effects (Either Text Value)
memoryListCore args = do
  result <-
    suspendEffect @Memory.MemoryList
      ( Memory.MemoryListRequest
          { Proto.memoryListRequestRunId = "",
            Proto.memoryListRequestAgentId = "",
            Proto.memoryListRequestIssueId = maybe 0 fromIntegral (memoryListArgsIssueId args),
            Proto.memoryListRequestKind = maybe 0 protoKind (memoryListArgsKind args),
            Proto.memoryListRequestMinImportance = maybe 0 fromIntegral (memoryListArgsMinImportance args),
            Proto.memoryListRequestLimit = maybe 0 fromIntegral (memoryListArgsLimit args)
          }
      )
  pure $ case result of
    Left err -> Left ("memory.list failed: " <> T.pack (show err))
    Right response ->
      Right $ object ["records" .= map memoryRecordValue (V.toList (PBT.nestedvec (Proto.memoryListResponseRecords response)))]

memoryRecordValue :: Proto.MemoryRecord -> Value
memoryRecordValue record =
  object
    [ "id" .= Proto.memoryRecordId record,
      "run_id" .= strictText (Proto.memoryRecordRunId record),
      "agent_id" .= strictText (Proto.memoryRecordAgentId record),
      "birth_branch" .= strictText (Proto.memoryRecordBirthBranch record),
      "issue_id" .= Proto.memoryRecordIssueId record,
      "kind" .= kindTextFromProto (Proto.memoryRecordKind record),
      "importance" .= Proto.memoryRecordImportance record,
      "summary" .= strictText (Proto.memoryRecordSummary record),
      "detail" .= strictText (Proto.memoryRecordDetail record),
      "created_at" .= Proto.memoryRecordCreatedAt record,
      "supersedes_id" .= Proto.memoryRecordSupersedesId record,
      "metadata_json" .= strictText (Proto.memoryRecordMetadataJson record)
    ]

kindTextFromProto :: Int32 -> Text
kindTextFromProto value =
  case value of
    0 -> "unspecified"
    1 -> "original_plan"
    2 -> "wave_plan"
    3 -> "spawned_child"
    4 -> "child_handoff"
    5 -> "blocker"
    6 -> "decision"
    7 -> "review_feedback"
    8 -> "fix_direction"
    9 -> "merge_result"
    10 -> "ci_result"
    11 -> "next_action"
    12 -> "human_clarification"
    13 -> "session_summary"
    other -> "unknown:" <> T.pack (show other)

strictText :: TL.Text -> Text
strictText = TL.toStrict

data ContinuationBriefArgs = ContinuationBriefArgs
  deriving (Generic, Show)

instance FromJSON ContinuationBriefArgs where
  parseJSON = withObject "ContinuationBriefArgs" $ \_ -> pure ContinuationBriefArgs

instance ToJSON ContinuationBriefArgs where
  toJSON ContinuationBriefArgs = object []

continuationBriefDescription :: Text
continuationBriefDescription = "Render the deterministic continuation brief for the current root or TL session."

continuationBriefSchema :: Aeson.Object
continuationBriefSchema = genericToolSchemaWith @ContinuationBriefArgs []

continuationBriefCore :: ContinuationBriefArgs -> Eff Effects (Either Text Value)
continuationBriefCore _ = do
  result <- suspendEffect @Memory.MemoryBrief Memory.MemoryBriefRequest
  pure $ case result of
    Left err -> Left ("memory.brief failed: " <> T.pack (show err))
    Right response -> Right $ object ["brief" .= strictText (Memory.memoryBriefResponseMarkdown response)]

data MemoryAppend = MemoryAppend

instance MCPTool MemoryAppend where
  type ToolArgs MemoryAppend = MemoryAppendArgs
  toolName = "memory_append"
  toolDescription = memoryAppendDescription
  toolSchema = memoryAppendSchema
  toolHandlerEff args = resultToTool (memoryAppendCore args)

data MemoryList = MemoryList

instance MCPTool MemoryList where
  type ToolArgs MemoryList = MemoryListArgs
  toolName = "memory_list"
  toolDescription = memoryListDescription
  toolSchema = memoryListSchema
  toolHandlerEff args = resultToTool (memoryListCore args)

data ContinuationBrief = ContinuationBrief

instance MCPTool ContinuationBrief where
  type ToolArgs ContinuationBrief = ContinuationBriefArgs
  toolName = "continuation_brief"
  toolDescription = continuationBriefDescription
  toolSchema = continuationBriefSchema
  toolHandlerEff args = resultToTool (continuationBriefCore args)

resultToTool :: Eff Effects (Either Text Value) -> Eff Effects MCPCallOutput
resultToTool action = do
  result <- action
  pure $ either errorResult successResult result

{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Typed effects and proto3-compatible wire records for session memory.
module ExoMonad.Effects.Memory
  ( MemoryAppend,
    MemoryList,
    MemoryBrief,
    MemoryKind (..),
    MemoryRecord (..),
    MemoryAppendRequest (..),
    MemoryAppendResponse (..),
    MemoryListRequest (..),
    MemoryListResponse (..),
    MemoryBriefRequest (..),
    MemoryBriefResponse (..),
  )
where

import Data.Int (Int32, Int64)
import Data.Text.Lazy qualified as TL
import ExoMonad.Effect.Class (Effect (..))
import GHC.Generics (Generic)
import Proto3.Suite.Class qualified as Proto
import Proto3.Suite.Types qualified as PBT

data MemoryKind
  = MemoryKindMEMORY_KIND_UNSPECIFIED
  | MemoryKindORIGINAL_PLAN
  | MemoryKindWAVE_PLAN
  | MemoryKindSPAWNED_CHILD
  | MemoryKindCHILD_HANDOFF
  | MemoryKindBLOCKER
  | MemoryKindDECISION
  | MemoryKindREVIEW_FEEDBACK
  | MemoryKindFIX_DIRECTION
  | MemoryKindMERGE_RESULT
  | MemoryKindCI_RESULT
  | MemoryKindNEXT_ACTION
  | MemoryKindHUMAN_CLARIFICATION
  | MemoryKindSESSION_SUMMARY
  deriving (Eq, Generic, Show)

data MemoryRecord = MemoryRecord
  { memoryRecordId :: Int64,
    memoryRecordRunId :: TL.Text,
    memoryRecordAgentId :: TL.Text,
    memoryRecordBirthBranch :: TL.Text,
    memoryRecordIssueId :: Int64,
    memoryRecordKind :: Int32,
    memoryRecordImportance :: Int32,
    memoryRecordSummary :: TL.Text,
    memoryRecordDetail :: TL.Text,
    memoryRecordCreatedAt :: Int64,
    memoryRecordSupersedesId :: Int64,
    memoryRecordMetadataJson :: TL.Text
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryRecord

instance Proto.HasDefault MemoryRecord

instance Proto.Message MemoryRecord

data MemoryAppendRequest = MemoryAppendRequest
  { memoryAppendRequestRunId :: TL.Text,
    memoryAppendRequestAgentId :: TL.Text,
    memoryAppendRequestBirthBranch :: TL.Text,
    memoryAppendRequestIssueId :: Int64,
    memoryAppendRequestKind :: Int32,
    memoryAppendRequestImportance :: Int32,
    memoryAppendRequestSummary :: TL.Text,
    memoryAppendRequestDetail :: TL.Text,
    memoryAppendRequestSupersedesId :: Int64,
    memoryAppendRequestMetadataJson :: TL.Text
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryAppendRequest

instance Proto.HasDefault MemoryAppendRequest

instance Proto.Message MemoryAppendRequest

data MemoryAppendResponse = MemoryAppendResponse
  { memoryAppendResponseId :: Int64
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryAppendResponse

instance Proto.HasDefault MemoryAppendResponse

instance Proto.Message MemoryAppendResponse

data MemoryListRequest = MemoryListRequest
  { memoryListRequestRunId :: TL.Text,
    memoryListRequestAgentId :: TL.Text,
    memoryListRequestIssueId :: Int64,
    memoryListRequestKind :: Int32,
    memoryListRequestMinImportance :: Int32,
    memoryListRequestLimit :: Int32
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryListRequest

instance Proto.HasDefault MemoryListRequest

instance Proto.Message MemoryListRequest

data MemoryListResponse = MemoryListResponse
  { memoryListResponseRecords :: PBT.NestedVec MemoryRecord
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryListResponse

instance Proto.HasDefault MemoryListResponse

instance Proto.Message MemoryListResponse

data MemoryBriefRequest = MemoryBriefRequest
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryBriefRequest

instance Proto.HasDefault MemoryBriefRequest

instance Proto.Message MemoryBriefRequest

data MemoryBriefResponse = MemoryBriefResponse
  { memoryBriefResponseMarkdown :: TL.Text
  }
  deriving (Eq, Generic, Show)

instance Proto.Named MemoryBriefResponse

instance Proto.HasDefault MemoryBriefResponse

instance Proto.Message MemoryBriefResponse

data MemoryAppend

instance Effect MemoryAppend where
  type Input MemoryAppend = MemoryAppendRequest
  type Output MemoryAppend = MemoryAppendResponse
  effectId = "memory.append"

data MemoryList

instance Effect MemoryList where
  type Input MemoryList = MemoryListRequest
  type Output MemoryList = MemoryListResponse
  effectId = "memory.list"

data MemoryBrief

instance Effect MemoryBrief where
  type Input MemoryBrief = MemoryBriefRequest
  type Output MemoryBrief = MemoryBriefResponse
  effectId = "memory.brief"

{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | Agent effects for spawning, cleaning up, and listing agents.
--
-- All effects are dispatched via the @agent@ namespace.
-- Request and response types are proto-generated from @proto/effects/agent.proto@.
module ExoMonad.Effects.Agent
  ( -- * Effect Types
    AgentSpawn,
    AgentSpawnBatch,
    AgentSpawnGeminiTeammate,
    AgentSpawnWorker,
    AgentSpawnSubtree,
    AgentSpawnReviewer,
    AgentCleanupReviewerLeaf,
    AgentRestartReview,
    AgentWatcherPrState,
    AgentSpawnLeafSubtree,
    AgentSpawnAcp,
    AgentCleanup,
    AgentDisposeOrphan,
    AgentCleanupBatch,
    AgentCleanupMerged,
    AgentList,
    AgentCloseSelf,
    AgentCloseWorkerPane,
    AgentCloseReviewerWindow,
    AgentCloseIssueAndCleanup,

    -- * Re-exported proto types
    module Effects.Agent,
  )
where

import Effects.Agent
import Effects.EffectError (EffectError)
import ExoMonad.Effect.Class (Effect (..))

-- ============================================================================
-- Effect phantom types + instances
-- ============================================================================

data AgentSpawn

instance Effect AgentSpawn where
  type Input AgentSpawn = SpawnRequest
  type Output AgentSpawn = SpawnResponse
  effectId = "agent.spawn"

data AgentSpawnBatch

instance Effect AgentSpawnBatch where
  type Input AgentSpawnBatch = SpawnBatchRequest
  type Output AgentSpawnBatch = SpawnBatchResponse
  effectId = "agent.spawn_batch"

data AgentSpawnGeminiTeammate

instance Effect AgentSpawnGeminiTeammate where
  type Input AgentSpawnGeminiTeammate = SpawnGeminiTeammateRequest
  type Output AgentSpawnGeminiTeammate = SpawnGeminiTeammateResponse
  effectId = "agent.spawn_gemini_teammate"

data AgentSpawnWorker

instance Effect AgentSpawnWorker where
  type Input AgentSpawnWorker = SpawnWorkerRequest
  type Output AgentSpawnWorker = SpawnWorkerResponse
  effectId = "agent.spawn_worker"

data AgentSpawnSubtree

instance Effect AgentSpawnSubtree where
  type Input AgentSpawnSubtree = SpawnSubtreeRequest
  type Output AgentSpawnSubtree = SpawnSubtreeResponse
  effectId = "agent.spawn_subtree"

data AgentSpawnReviewer

instance Effect AgentSpawnReviewer where
  type Input AgentSpawnReviewer = SpawnReviewerRequest
  type Output AgentSpawnReviewer = SpawnReviewerResponse
  effectId = "agent.spawn_reviewer"

data AgentCleanupReviewerLeaf

instance Effect AgentCleanupReviewerLeaf where
  type Input AgentCleanupReviewerLeaf = CleanupReviewerLeafRequest
  type Output AgentCleanupReviewerLeaf = CleanupReviewerLeafResponse
  effectId = "agent.cleanup_reviewer_leaf"

data AgentRestartReview

instance Effect AgentRestartReview where
  type Input AgentRestartReview = RestartReviewRequest
  type Output AgentRestartReview = RestartReviewResponse
  effectId = "agent.restart_review"

data AgentWatcherPrState

instance Effect AgentWatcherPrState where
  type Input AgentWatcherPrState = WatcherPrStateRequest
  type Output AgentWatcherPrState = WatcherPrStateResponse
  effectId = "agent.watcher_pr_state"

data AgentSpawnLeafSubtree

instance Effect AgentSpawnLeafSubtree where
  type Input AgentSpawnLeafSubtree = SpawnLeafSubtreeRequest
  type Output AgentSpawnLeafSubtree = SpawnLeafSubtreeResponse
  effectId = "agent.spawn_leaf_subtree"

data AgentSpawnAcp

instance Effect AgentSpawnAcp where
  type Input AgentSpawnAcp = SpawnAcpRequest
  type Output AgentSpawnAcp = SpawnAcpResponse
  effectId = "agent.spawn_acp"

data AgentCleanup

instance Effect AgentCleanup where
  type Input AgentCleanup = CleanupRequest
  type Output AgentCleanup = CleanupResponse
  effectId = "agent.cleanup"

data AgentDisposeOrphan

instance Effect AgentDisposeOrphan where
  type Input AgentDisposeOrphan = DisposeOrphanRequest
  type Output AgentDisposeOrphan = DisposeOrphanResponse
  effectId = "agent.dispose_orphan"

data AgentCleanupBatch

instance Effect AgentCleanupBatch where
  type Input AgentCleanupBatch = CleanupBatchRequest
  type Output AgentCleanupBatch = CleanupBatchResponse
  effectId = "agent.cleanup_batch"

data AgentCleanupMerged

instance Effect AgentCleanupMerged where
  type Input AgentCleanupMerged = CleanupMergedRequest
  type Output AgentCleanupMerged = CleanupMergedResponse
  effectId = "agent.cleanup_merged"

data AgentList

instance Effect AgentList where
  type Input AgentList = ListRequest
  type Output AgentList = ListResponse
  effectId = "agent.list"

data AgentCloseSelf

instance Effect AgentCloseSelf where
  type Input AgentCloseSelf = CloseSelfRequest
  type Output AgentCloseSelf = CloseSelfResponse
  effectId = "agent.close_self"

data AgentCloseWorkerPane

instance Effect AgentCloseWorkerPane where
  type Input AgentCloseWorkerPane = CloseWorkerPaneRequest
  type Output AgentCloseWorkerPane = CloseWorkerPaneResponse
  effectId = "agent.close_worker_pane"

data AgentCloseReviewerWindow

instance Effect AgentCloseReviewerWindow where
  type Input AgentCloseReviewerWindow = CloseReviewerWindowRequest
  type Output AgentCloseReviewerWindow = CloseReviewerWindowResponse
  effectId = "agent.close_reviewer_window"

data AgentCloseIssueAndCleanup

instance Effect AgentCloseIssueAndCleanup where
  type Input AgentCloseIssueAndCleanup = CloseIssueAndCleanupRequest
  type Output AgentCloseIssueAndCleanup = CloseIssueAndCleanupResponse
  effectId = "agent.close_issue_and_cleanup"

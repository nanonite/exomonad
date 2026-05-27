{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeFamilies #-}

-- | File PR effects for creating pull requests from file changes.
--
-- All effects are dispatched via the @file_pr@ namespace.
-- Request and response types are proto-generated from @proto/effects/file_pr.proto@.
module ExoMonad.Effects.FilePR
  ( -- * Effect Types
    FilePRFilePr,
    FilePRLocalPrGet,
    FilePRLocalPrGetForBranch,
    FilePRSubmitReview,

    -- * Re-exported proto types
    module Effects.FilePr,
  )
where

import Effects.FilePr
import ExoMonad.Effect.Class (Effect (..))

-- ============================================================================
-- Effect phantom types + instances
-- ============================================================================

data FilePRFilePr

instance Effect FilePRFilePr where
  type Input FilePRFilePr = FilePrRequest
  type Output FilePRFilePr = FilePrResponse
  effectId = "file_pr.file_pr"

data FilePRLocalPrGet

instance Effect FilePRLocalPrGet where
  type Input FilePRLocalPrGet = LocalPrGetRequest
  type Output FilePRLocalPrGet = LocalPrResponse
  effectId = "file_pr.local_pr_get"

data FilePRLocalPrGetForBranch

instance Effect FilePRLocalPrGetForBranch where
  type Input FilePRLocalPrGetForBranch = LocalPrGetForBranchRequest
  type Output FilePRLocalPrGetForBranch = LocalPrResponse
  effectId = "file_pr.local_pr_get_for_branch"

data FilePRSubmitReview

instance Effect FilePRSubmitReview where
  type Input FilePRSubmitReview = SubmitReviewRequest
  type Output FilePRSubmitReview = SubmitReviewResponse
  effectId = "file_pr.submit_review"

{-# LANGUAGE OverloadedStrings #-}

-- | Worker stop hook.
module WorkerStopCheck (workerStopCheck) where

import Control.Monad.Freer (Eff)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Git qualified as Git
import ExoMonad.Effects.Git (GitGetStatus)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Types (StopHookOutput, allowStopResponse, blockStopResponse)
import ExoMonad.Types (Effects)

workerStopCheck :: Eff Effects StopHookOutput
workerStopCheck = do
  statusResult <- suspendEffect @GitGetStatus (Git.GetStatusRequest {Git.getStatusRequestWorkingDir = "."})
  case statusResult of
    Right status
      | not (null (Git.getStatusResponseDirtyFiles status) && null (Git.getStatusResponseStagedFiles status)) ->
          pure $ blockStopResponse (dirtyWorkerMessage status)
    _ -> pure allowStopResponse

dirtyWorkerMessage :: Git.GetStatusResponse -> Text
dirtyWorkerMessage status =
  "Worker has uncommitted changes:\n"
    <> formatStatus status
    <> "\nCommit under your own identity (`git add . && git commit -m \"...\"`) or discard explicitly via `discard_worker_output` before ending the session."

formatStatus :: Git.GetStatusResponse -> Text
formatStatus status =
  T.unlines $
    map (("staged: " <>) . TL.toStrict) (V.toList (Git.getStatusResponseStagedFiles status))
      <> map (("dirty: " <>) . TL.toStrict) (V.toList (Git.getStatusResponseDirtyFiles status))

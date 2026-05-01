{-# LANGUAGE OverloadedStrings #-}

-- | Worker stop hook: blocks exit if active chainlink locks exist.
module WorkerStopCheck (workerStopCheck) where

import Control.Monad.Freer (Eff)
import Data.Map qualified as Map
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Process qualified as Proc
import ExoMonad.Guest.Tools.Chainlink.Pure (hasActiveLocks, buildLocksListArgs)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Types (Effects)
import ExoMonad.Guest.Types (StopHookOutput (..), blockStopResponse, allowStopResponse)

-- | Stop hook that checks for active chainlink locks.
-- If any locks are held by this agent, blocks exit with a nudge.
workerStopCheck :: Eff Effects StopHookOutput
workerStopCheck = do
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "chainlink",
            Proc.runRequestArgs = V.fromList (TL.pack <$> buildLocksListArgs),
            Proc.runRequestWorkingDir = ".",
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = 15000
          }
      )
  case result of
    Left _err -> pure allowStopResponse
    Right resp -> do
      let stdout = TL.toStrict (Proc.runResponseStdout resp)
      if hasActiveLocks stdout
        then pure $ blockStopResponse
          "You have active chainlink locks. Call chainlink_issue_close before exiting."
        else pure allowStopResponse

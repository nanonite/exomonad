-- | Worker stop hook.
module WorkerStopCheck (workerStopCheck) where

import Control.Monad.Freer (Eff)
import ExoMonad.Types (Effects)
import ExoMonad.Guest.Types (StopHookOutput, allowStopResponse)

-- Chainlink locks are not part of the ExoMonad workflow. Worker completion is
-- reported through session_end plus notify_parent, and close authority belongs
-- to the parent coordinator.
workerStopCheck :: Eff Effects StopHookOutput
workerStopCheck = pure allowStopResponse

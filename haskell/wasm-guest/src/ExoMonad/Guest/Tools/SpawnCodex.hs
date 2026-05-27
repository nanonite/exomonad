-- | Dedicated Codex spawn tool wrapper.
module ExoMonad.Guest.Tools.SpawnCodex
  ( SpawnCodex,
    spawnCodexDescription,
    spawnCodexSchema,
    handleSpawnCodex,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import ExoMonad.Guest.Effects.AgentControl qualified as AC
import ExoMonad.Guest.Tools.Spawn
  ( SpawnLeafSubtreeArgs (..),
    spawnLeafSubtreeCore,
    spawnLeafSubtreeSchema,
  )
import ExoMonad.Guest.Types (Effects)

data SpawnCodex

spawnCodexDescription :: Text
spawnCodexDescription =
  "Fork a Codex leaf agent into its own worktree and tmux window. Gets dev role (files PR, cannot spawn children). After spawning, return immediately."

spawnCodexSchema :: Aeson.Object
spawnCodexSchema = spawnLeafSubtreeSchema

handleSpawnCodex :: SpawnLeafSubtreeArgs -> Eff Effects (Either Text (Text, AC.SpawnResult))
handleSpawnCodex args =
  spawnLeafSubtreeCore args {slsAgentType = Just AC.Codex}

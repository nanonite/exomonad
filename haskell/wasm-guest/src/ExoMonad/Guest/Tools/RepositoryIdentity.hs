{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.RepositoryIdentity
  ( RepositoryIdentity (..),
    RepositoryIdentityArgs (..),
    repositoryIdentityDescription,
    repositoryIdentitySchema,
    repositoryIdentityCore,
    repositoryIdentityResponseValue,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), object, withObject, (.=))
import Data.Aeson qualified as Aeson
import Data.Text (Text)
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Effects.Agent qualified as Agent
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import ExoMonad.Guest.Tools.Spawn (spawnErrorMessage)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data RepositoryIdentity

data RepositoryIdentityArgs = RepositoryIdentityArgs
  deriving (Show, Eq, Generic)

instance FromJSON RepositoryIdentityArgs where
  parseJSON = withObject "RepositoryIdentityArgs" $ \_ -> pure RepositoryIdentityArgs

repositoryIdentityDescription :: Text
repositoryIdentityDescription =
  "Resolve the run's repository identity (owner, repo, base branch, forge host) from the pinned git remote. Read-only, no arguments, one call per continuation. Fails closed when the remote is missing, ambiguous, or unparseable. This is static run configuration, not a per-PR observation -- it is never sourced from watcher_pr_state."

repositoryIdentitySchema :: Aeson.Object
repositoryIdentitySchema = genericToolSchemaWith @RepositoryIdentityArgs []

repositoryIdentityResponseValue :: PA.RepositoryIdentityResponse -> Aeson.Value
repositoryIdentityResponseValue resp =
  object
    [ "success" .= True,
      "owner" .= lazyText (PA.repositoryIdentityResponseOwner resp),
      "repo" .= lazyText (PA.repositoryIdentityResponseRepo resp),
      "base_branch" .= lazyText (PA.repositoryIdentityResponseBaseBranch resp),
      "forge_host" .= lazyText (PA.repositoryIdentityResponseForgeHost resp),
      "remote_url" .= lazyText (PA.repositoryIdentityResponseRemoteUrl resp),
      "remote_name" .= lazyText (PA.repositoryIdentityResponseRemoteName resp)
    ]

repositoryIdentityCore :: RepositoryIdentityArgs -> Eff Effects (Either Text Aeson.Value)
repositoryIdentityCore _ = do
  result <- suspendEffect @Agent.AgentRepositoryIdentity PA.RepositoryIdentityRequest
  pure $ case result of
    Left err -> Left (spawnErrorMessage err)
    Right resp
      | not (PA.repositoryIdentityResponseSuccess resp) ->
          Left (lazyText (PA.repositoryIdentityResponseError resp))
      | otherwise -> Right (repositoryIdentityResponseValue resp)

instance MCPTool RepositoryIdentity where
  type ToolArgs RepositoryIdentity = RepositoryIdentityArgs
  toolName = "repository_identity"
  toolDescription = repositoryIdentityDescription
  toolSchema = repositoryIdentitySchema
  toolHandlerEff args = do
    result <- repositoryIdentityCore args
    pure $ case result of
      Left err -> errorResult err
      Right value -> successResult value

lazyText :: TL.Text -> Text
lazyText = TL.toStrict

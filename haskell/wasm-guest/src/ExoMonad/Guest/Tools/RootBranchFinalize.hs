{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

-- | The root-only finalization boundary for a checked-out branch.
module ExoMonad.Guest.Tools.RootBranchFinalize
  ( RootBranchFinalize,
    RootBranchFinalizeArgs (..),
    rootBranchFinalizeCore,
    rootBranchFinalizeDescription,
    rootBranchFinalizeSchema,
    rootBranchFinalizeMergeArgs,
  )
where

import Control.Monad.Freer (Eff, Member)
import Data.Aeson (FromJSON (..), Value, object, withObject, (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Map qualified as Map
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as V
import Effects.Process qualified as Proc
import ExoMonad.Effects.Process (ProcessRun)
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tool.Suspend.Types (SuspendYield)
import ExoMonad.Guest.Tool.SuspendEffect (suspendEffect)
import GHC.Generics (Generic)

data RootBranchFinalize

data RootBranchFinalizeArgs = RootBranchFinalizeArgs
  { rbfBranch :: Text,
    rbfWorkingDir :: Maybe Text
  }
  deriving (Show, Eq, Generic)

instance FromJSON RootBranchFinalizeArgs where
  parseJSON = withObject "RootBranchFinalizeArgs" $ \v ->
    RootBranchFinalizeArgs
      <$> v .: "branch"
      <*> v .:? "working_dir"

rootBranchFinalizeDescription :: Text
rootBranchFinalizeDescription =
  "Finalize the root branch by fetching its remote and applying a verified fast-forward-only update."

rootBranchFinalizeSchema :: Aeson.Object
rootBranchFinalizeSchema =
  genericToolSchemaWith @RootBranchFinalizeArgs
    [ ("branch", "Root branch that must be checked out."),
      ("working_dir", "Optional repository working directory.")
    ]

instance MCPTool RootBranchFinalize where
  type ToolArgs RootBranchFinalize = RootBranchFinalizeArgs
  toolName = "root_branch_finalize"
  toolDescription = rootBranchFinalizeDescription
  toolSchema = rootBranchFinalizeSchema
  toolHandlerEff args = either errorResult successResult <$> rootBranchFinalizeCore args

rootBranchFinalizeCore :: (Member SuspendYield effs) => RootBranchFinalizeArgs -> Eff effs (Either Text Value)
rootBranchFinalizeCore args
  | not (validToken (rbfBranch args)) = pure $ Left "branch is required"
  | otherwise = do
      branch <- runGitAt (rbfWorkingDir args) ["branch", "--show-current"]
      case branch of
        Left err -> pure $ Left err
        Right current
          | T.strip current /= rbfBranch args ->
              pure $ Left "root finalization requires the requested branch to be checked out"
          | otherwise -> do
              status <- runGitAt (rbfWorkingDir args) ["status", "--porcelain"]
              case status of
                Left err -> pure $ Left err
                Right dirty
                  | not (T.null (T.strip dirty)) ->
                      pure $ Left "root finalization requires a clean checkout"
                  | otherwise -> do
                      localBefore <- runGitAt (rbfWorkingDir args) ["rev-parse", "HEAD"]
                      fetched <- runGitAt (rbfWorkingDir args) ["fetch", "--prune", "origin", rbfBranch args]
                      case (localBefore, fetched) of
                        (Left err, _) -> pure $ Left err
                        (_, Left err) -> pure $ Left err
                        (Right before, Right _) -> do
                          remote <- runGitAt (rbfWorkingDir args) ["rev-parse", remoteRef (rbfBranch args)]
                          case remote of
                            Left err -> pure $ Left err
                            Right remoteHead -> do
                              ancestry <-
                                runGitAt
                                  (rbfWorkingDir args)
                                  ["merge-base", "--is-ancestor", before, remoteHead]
                              case ancestry of
                                Left err -> pure $ Left ("root branch is not fast-forwardable: " <> err)
                                Right _ -> do
                                  merged <-
                                    runGitAt
                                      (rbfWorkingDir args)
                                      (rootBranchFinalizeMergeArgs (rbfBranch args))
                                  case merged of
                                    Left err -> pure $ Left err
                                    Right _ -> do
                                      localAfter <- runGitAt (rbfWorkingDir args) ["rev-parse", "HEAD"]
                                      case localAfter of
                                        Left err -> pure $ Left err
                                        Right finalHead
                                          | finalHead /= remoteHead ->
                                              pure $ Left "root finalization produced a divergent local and remote head"
                                          | otherwise ->
                                              pure $
                                                Right
                                                  ( object
                                                      [ "branch" .= rbfBranch args,
                                                        "local_head_sha" .= finalHead,
                                                        "remote_head_sha" .= remoteHead,
                                                        "ancestry_proof" .= ("ancestor:" <> before <> "->" <> remoteHead),
                                                        "fast_forward" .= True
                                                      ]
                                                  )

rootBranchFinalizeMergeArgs :: Text -> [Text]
rootBranchFinalizeMergeArgs branch = ["merge", "--ff-only", remoteRef branch]

runGitAt :: (Member SuspendYield effs) => Maybe Text -> [Text] -> Eff effs (Either Text Text)
runGitAt workingDir args = do
  result <-
    suspendEffect @ProcessRun
      ( Proc.RunRequest
          { Proc.runRequestCommand = "git",
            Proc.runRequestArgs = V.fromList (TL.fromStrict <$> args),
            Proc.runRequestWorkingDir = maybe "." TL.fromStrict workingDir,
            Proc.runRequestEnv = Map.empty,
            Proc.runRequestTimeoutMs = 120000
          }
      )
  case result of
    Left err -> pure $ Left ("git effect failed: " <> T.pack (show err))
    Right response ->
      pure
        ( if Proc.runResponseExitCode response == 0
            then Right (T.strip (TL.toStrict (Proc.runResponseStdout response)))
            else Left ("git command failed: " <> T.strip (TL.toStrict (Proc.runResponseStderr response)))
        )

remoteRef :: Text -> Text
remoteRef branch = "origin/" <> branch

validToken :: Text -> Bool
validToken value =
  let stripped = T.strip value
   in not (T.null stripped)
        && stripped == value
        && not (T.isPrefixOf "-" value)
        && not (T.any (\char -> char == '\0' || char == '\n' || char == '\r' || char == ' ') value)

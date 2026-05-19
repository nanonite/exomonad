{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE OverloadedStrings #-}
{-# LANGUAGE TypeApplications #-}
{-# LANGUAGE TypeFamilies #-}

module ExoMonad.Guest.Tools.DisposeLeaf
  ( DisposeLeaf (..),
    DisposeLeafArgs (..),
    disposeLeafDescription,
    disposeLeafSchema,
    disposeLeafCore,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (FromJSON (..), ToJSON (..), object, withObject, (.!=), (.:), (.:?), (.=))
import Data.Aeson qualified as Aeson
import Data.Char (isDigit)
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Tool.Class (MCPTool (..), errorResult, successResult)
import ExoMonad.Guest.Tool.Schema (genericToolSchemaWith)
import ExoMonad.Guest.Tools.Chainlink (ChainlinkIssueCloseArgs (..), chainlinkIssueCloseCore)
import ExoMonad.Guest.Types (Effects)
import GHC.Generics (Generic)

data DisposeLeafArgs = DisposeLeafArgs
  { dlaName :: Text,
    dlaReason :: Text,
    dlaForce :: Bool
  }
  deriving (Generic, Show)

instance FromJSON DisposeLeafArgs where
  parseJSON = withObject "DisposeLeafArgs" $ \v ->
    DisposeLeafArgs
      <$> v .: "name"
      <*> v .: "reason"
      <*> v .:? "force" .!= False

instance ToJSON DisposeLeafArgs where
  toJSON args =
    object
      [ "name" .= dlaName args,
        "reason" .= dlaReason args,
        "force" .= dlaForce args
      ]

disposeLeafDescription :: Text
disposeLeafDescription =
  "Dispose a dev leaf by closing its assigned Chainlink issue. The IssueClosed event performs the actual leaf teardown."

disposeLeafSchema :: Aeson.Object
disposeLeafSchema =
  genericToolSchemaWith @DisposeLeafArgs
    [ ("name", "The leaf agent name, usually containing issue-<id>"),
      ("reason", "Human-readable disposal reason recorded in the close summary"),
      ("force", "Force disposal for a hung orphan. Defaults to false.")
    ]

disposeLeafCore :: DisposeLeafArgs -> Eff Effects (Either Text Aeson.Value)
disposeLeafCore args =
  case inferIssueId (dlaName args) of
    Just issueId -> do
      let summary = Just ("Disposed by TL: " <> dlaReason args)
      result <- chainlinkIssueCloseCore (ChainlinkIssueCloseArgs issueId summary (dlaForce args))
      case result of
        Left err -> pure $ Left err
        Right _ -> pure $ Right $ object ["success" .= True, "agent" .= dlaName args, "issue_id" .= issueId, "force" .= dlaForce args]
    Nothing
      | dlaForce args ->
          pure $ Right $ object ["success" .= True, "agent" .= dlaName args, "issue_id" .= (0 :: Int), "force" .= True, "reason" .= dlaReason args]
      | otherwise -> pure $ Left "Could not infer a Chainlink issue id from the leaf name. Pass force=true only for a genuine orphan."

inferIssueId :: Text -> Maybe Int
inferIssueId name = firstDigitsAfterIssue (T.unpack name)

firstDigitsAfterIssue :: String -> Maybe Int
firstDigitsAfterIssue [] = Nothing
firstDigitsAfterIssue s@('i' : 's' : 's' : 'u' : 'e' : '-' : rest) = parseDigits rest
firstDigitsAfterIssue (_ : rest) = firstDigitsAfterIssue rest

parseDigits :: String -> Maybe Int
parseDigits rest =
  case span isDigit rest of
    ([], _) -> Nothing
    (digits, _) -> Just (read digits)

data DisposeLeaf

instance MCPTool DisposeLeaf where
  type ToolArgs DisposeLeaf = DisposeLeafArgs
  toolName = "dispose_leaf"
  toolDescription = disposeLeafDescription
  toolSchema = disposeLeafSchema
  toolHandlerEff args = do
    result <- disposeLeafCore args
    case result of
      Left err -> pure $ errorResult err
      Right value -> pure $ successResult value

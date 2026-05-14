{-# LANGUAGE OverloadedStrings #-}

-- | Shared hook policy guards for agent shell commands.
module HookPolicy
  ( blockGhCommand,
    blockChainlinkSqliteCommand,
    preToolUseWithGhBlock,
  )
where

import Control.Monad.Freer (Eff)
import Data.Aeson (Value (..))
import Data.Aeson.KeyMap qualified as KM
import Data.Text (Text)
import Data.Text qualified as T
import ExoMonad.Guest.Types (HookInput (..), HookOutput, denyResponse)
import ExoMonad.Types (Effects)

blockGhCommand :: HookInput -> Maybe Text
blockGhCommand hookInput =
  commandFromHookInput hookInput >>= \cmd ->
    if containsGhToken cmd
      then
        Just $
          "BLOCKED: Do not run gh commands from agents. "
            <> "Use ExoMonad MCP tools such as file_pr, merge_pr, and local review flow instead."
      else Nothing

blockChainlinkSqliteCommand :: HookInput -> Maybe Text
blockChainlinkSqliteCommand hookInput =
  commandFromHookInput hookInput >>= \cmd ->
    if accessesChainlinkSqlite cmd
      then
        Just $
          "BLOCKED: Do not access Chainlink sqlite databases directly. "
            <> "Use the scoped Chainlink MCP tools instead."
      else Nothing

commandFromHookInput :: HookInput -> Maybe Text
commandFromHookInput hookInput =
  case hiToolInput hookInput of
    Just (Object obj)
      | Just (String cmd) <- KM.lookup "command" obj ->
          Just cmd
    _ -> Nothing

preToolUseWithGhBlock ::
  (HookInput -> Eff Effects HookOutput) ->
  HookInput ->
  Eff Effects HookOutput
preToolUseWithGhBlock next hookInput =
  case blockGhCommand hookInput of
    Just reason -> pure (denyResponse reason)
    Nothing ->
      case blockChainlinkSqliteCommand hookInput of
        Just reason -> pure (denyResponse reason)
        Nothing -> next hookInput

containsGhToken :: Text -> Bool
containsGhToken cmd =
  "gh" `elem` T.words (T.map shellTokenSeparator (T.toCaseFold cmd))

accessesChainlinkSqlite :: Text -> Bool
accessesChainlinkSqlite cmd =
  let normalized = T.toCaseFold cmd
   in ".chainlink/issues.db" `T.isInfixOf` normalized
        || ("sqlite3" `elem` T.words (T.map shellTokenSeparator normalized)
              && ".chainlink" `T.isInfixOf` normalized
              && "issues.db" `T.isInfixOf` normalized)

shellTokenSeparator :: Char -> Char
shellTokenSeparator c
  | c `elem` (" \t\n\r;&|(){}[]<>`'\"/" :: String) = ' '
  | otherwise = c

{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.SpawnTest (spawnTests) where

import Data.Aeson qualified as Aeson
import Data.Aeson.Key (Key)
import Data.Aeson.KeyMap qualified as KeyMap
import Data.ByteString qualified as BS
import Data.Text.Lazy qualified as TL
import Effects.EffectError qualified as EffectError
import ExoMonad.Guest.Tools.Spawn
  ( SpawnToolFailure (..),
    spawnErrorOutput,
    spawnFailureFromEffectError,
  )
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, testCase)

spawnTests :: TestTree
spawnTests =
  testGroup
    "spawn error serialization"
    [ testCase "custom preflight code survives the MCP JSON projection" $ do
        let effectError =
              EffectError.EffectError . Just . EffectError.EffectErrorKindCustom $
                EffectError.Custom
                  { EffectError.customCode = "agent.spawn_precondition.dirty_worktree",
                    EffectError.customMessage = "host wording can change without changing the code",
                    EffectError.customData = BS.empty
                  }
            failure = spawnFailureFromEffectError effectError
            encoded = Aeson.toJSON (spawnErrorOutput failure)
        assertEqual
          "typed host error becomes a typed tool error"
          (Just (Aeson.toJSON ("agent.spawn_precondition.dirty_worktree" :: TL.Text)))
          (field "error_kind" encoded)
        assertEqual
          "message remains the host message"
          (Just (Aeson.toJSON ("host wording can change without changing the code" :: TL.Text)))
          (field "error" encoded),
      testCase "untyped errors remain ordinary tool errors" $ do
        let encoded = Aeson.toJSON (spawnErrorOutput (SpawnToolFailure Nothing "refused"))
        assertEqual "no fabricated error kind" (Just Aeson.Null) (field "error_kind" encoded)
        assertEqual "failure envelope" (Just (Aeson.toJSON (False :: Bool))) (field "success" encoded)
    ]

field :: Key -> Aeson.Value -> Maybe Aeson.Value
field key (Aeson.Object value) = KeyMap.lookup key value
field _ _ = Nothing

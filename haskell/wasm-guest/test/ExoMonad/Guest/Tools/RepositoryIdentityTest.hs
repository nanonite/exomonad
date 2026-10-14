{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.RepositoryIdentityTest (repositoryIdentityTests) where

import Data.Aeson (Value (..))
import Data.Aeson qualified as Aeson
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KeyMap
import Data.Text.Lazy qualified as TL
import Effects.Agent qualified as PA
import ExoMonad.Guest.Tools.RepositoryIdentity
  ( repositoryIdentityResponseValue,
    repositoryIdentitySchema,
  )
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, assertFailure, testCase)

successResponse :: PA.RepositoryIdentityResponse
successResponse =
  PA.RepositoryIdentityResponse
    { PA.repositoryIdentityResponseSuccess = True,
      PA.repositoryIdentityResponseError = "",
      PA.repositoryIdentityResponseRemoteUrl = TL.pack "https://forge.example.com/goya/beast.git"
    }

errorResponse :: PA.RepositoryIdentityResponse
errorResponse =
  PA.RepositoryIdentityResponse
    { PA.repositoryIdentityResponseSuccess = False,
      PA.repositoryIdentityResponseError = TL.pack "Repository remote URL is unavailable",
      PA.repositoryIdentityResponseRemoteUrl = ""
    }

field :: String -> Aeson.Object -> Maybe Value
field name = KeyMap.lookup (Key.fromString name)

repositoryIdentityTests :: TestTree
repositoryIdentityTests =
  testGroup
    "repository_identity"
    [ testCase "schema takes no arguments" $
        case Aeson.Object repositoryIdentitySchema of
          Object fields -> do
            assertEqual "type" (Just (Aeson.toJSON ("object" :: String))) (field "type" fields)
            case field "properties" fields of
              Just (Object props) -> assertEqual "properties" mempty props
              _ -> assertFailure "repository_identity schema must declare an (empty) properties object"
          _ -> assertFailure "repository_identity schema must serialize as a JSON object",
      testCase "success decode projects only the canonical repository URL" $
        case repositoryIdentityResponseValue successResponse of
          Object fields -> do
            assertEqual "success" (Just (Aeson.toJSON True)) (field "success" fields)
            assertEqual
              "remote_url"
              (Just (Aeson.toJSON ("https://forge.example.com/goya/beast.git" :: String)))
              (field "remote_url" fields)
            assertEqual "identity has no owner field" Nothing (field "owner" fields)
            assertEqual "identity has no branch field" Nothing (field "base_branch" fields)
          _ -> assertFailure "repository_identity projection must serialize as a JSON object",
      testCase "error decode carries the durable fail-closed reason, never a guessed identity" $ do
        assertEqual "success" False (PA.repositoryIdentityResponseSuccess errorResponse)
        assertEqual
          "error"
          (TL.pack "Repository remote URL is unavailable")
          (PA.repositoryIdentityResponseError errorResponse)
        assertEqual "URL is not guessed" "" (PA.repositoryIdentityResponseRemoteUrl errorResponse)
    ]

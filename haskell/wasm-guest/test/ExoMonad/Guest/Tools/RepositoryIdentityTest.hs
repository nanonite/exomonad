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
      PA.repositoryIdentityResponseOwner = TL.pack "goya",
      PA.repositoryIdentityResponseRepo = TL.pack "beast",
      PA.repositoryIdentityResponseBaseBranch = TL.pack "main",
      PA.repositoryIdentityResponseForgeHost = TL.pack "forge.example.com",
      PA.repositoryIdentityResponseRemoteUrl = TL.pack "https://forge.example.com/goya/beast.git",
      PA.repositoryIdentityResponseRemoteName = TL.pack "forgejo"
    }

errorResponse :: PA.RepositoryIdentityResponse
errorResponse =
  PA.RepositoryIdentityResponse
    { PA.repositoryIdentityResponseSuccess = False,
      PA.repositoryIdentityResponseError = TL.pack "Remote \"origin\"'s default branch is not recorded locally",
      PA.repositoryIdentityResponseOwner = "",
      PA.repositoryIdentityResponseRepo = "",
      PA.repositoryIdentityResponseBaseBranch = "",
      PA.repositoryIdentityResponseForgeHost = "",
      PA.repositoryIdentityResponseRemoteUrl = "",
      PA.repositoryIdentityResponseRemoteName = ""
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
      testCase "success decode projects owner/repo/base_branch/forge_host/remote metadata" $
        case repositoryIdentityResponseValue successResponse of
          Object fields -> do
            assertEqual "success" (Just (Aeson.toJSON True)) (field "success" fields)
            assertEqual "owner" (Just (Aeson.toJSON ("goya" :: String))) (field "owner" fields)
            assertEqual "repo" (Just (Aeson.toJSON ("beast" :: String))) (field "repo" fields)
            assertEqual "base_branch" (Just (Aeson.toJSON ("main" :: String))) (field "base_branch" fields)
            assertEqual "forge_host" (Just (Aeson.toJSON ("forge.example.com" :: String))) (field "forge_host" fields)
            assertEqual
              "remote_url"
              (Just (Aeson.toJSON ("https://forge.example.com/goya/beast.git" :: String)))
              (field "remote_url" fields)
            assertEqual "remote_name" (Just (Aeson.toJSON ("forgejo" :: String))) (field "remote_name" fields)
          _ -> assertFailure "repository_identity projection must serialize as a JSON object",
      testCase "error decode carries the durable fail-closed reason, never a guessed identity" $ do
        assertEqual "success" False (PA.repositoryIdentityResponseSuccess errorResponse)
        assertEqual
          "error"
          (TL.pack "Remote \"origin\"'s default branch is not recorded locally")
          (PA.repositoryIdentityResponseError errorResponse)
        assertEqual "owner is not guessed" "" (PA.repositoryIdentityResponseOwner errorResponse)
        assertEqual "base_branch is not guessed" "" (PA.repositoryIdentityResponseBaseBranch errorResponse)
    ]

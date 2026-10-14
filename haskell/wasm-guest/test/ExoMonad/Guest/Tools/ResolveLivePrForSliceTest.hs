{-# LANGUAGE OverloadedStrings #-}

module ExoMonad.Guest.Tools.ResolveLivePrForSliceTest
  ( resolveLivePrForSliceTests,
  )
where

import Control.Monad (filterM, forM_)
import Control.Monad.Freer (run)
import Control.Monad.Freer.Coroutine (Status (Continue, Done), runC)
import Data.Aeson (Value (..), toJSON)
import Data.Aeson qualified as Aeson
import Data.Aeson.Key qualified as Key
import Data.Aeson.KeyMap qualified as KeyMap
import Data.ByteString qualified as BS
import Data.ByteString.Lazy qualified as BL
import Data.List (sort)
import Data.Text (Text)
import Data.Text qualified as T
import Data.Text.Lazy qualified as TL
import Data.Vector qualified as Vector
import Data.Word (Word8)
import Effects.Agent qualified as PA
import Effects.EffectError qualified as EffectError
import Effects.Envelope qualified as Envelope
import ExoMonad.Guest.Tool.Class (successResult)
import ExoMonad.Guest.Tool.Suspend.Types (EffectRequest (..))
import ExoMonad.Guest.Tools.ResolveLivePrForSlice
  ( ResolveLivePrForSliceArgs (..),
    resolveLivePrForSliceCore,
    resolveLivePrForSliceResponseValue,
  )
import Proto3.Suite.Class (toLazyByteString)
import Proto3.Suite.Types qualified as Protobuf
import System.Directory (doesFileExist)
import Test.Tasty (TestTree, testGroup)
import Test.Tasty.HUnit (assertEqual, assertFailure, testCase)

resolveLivePrForSliceTests :: TestTree
resolveLivePrForSliceTests =
  testGroup
    "resolve_live_pr_for_slice"
    [ testCase "serializes typed publication using the shared Python contract fixture" $ do
        fixturePath <- findContractFixture
        fixtureBytes <- BS.readFile fixturePath
        let expected = successResult (resolveLivePrForSliceResponseValue liveResponse)
            decodedFixture = Aeson.eitherDecodeStrict' fixtureBytes :: Either String Value
            encodedProjection = BL.toStrict (Aeson.encode (toJSON expected))
            decodedProjection = Aeson.eitherDecodeStrict' encodedProjection :: Either String Value
        assertEqual "fixture decodes" (Right (toJSON expected)) decodedFixture
        assertEqual "production projection survives JSON bytes" decodedFixture decodedProjection
        case decodedFixture of
          Right (Object envelope) ->
            case KeyMap.lookup "result" envelope of
              Just (Object result) -> do
                assertEqual "success" (Just (toJSON True)) (KeyMap.lookup "success" result)
                assertEqual "slice_id" (Just (toJSON contractSliceId)) (KeyMap.lookup "slice_id" result)
                assertEqual "resolution" (Just (toJSON ("live" :: Text))) (KeyMap.lookup "resolution" result)
                assertEqual "pr_number" (Just (toJSON (46 :: Integer))) (KeyMap.lookup "pr_number" result)
                assertPublication contractPublication result
              _ -> assertFailure "fixture result must be an object"
          _ -> assertFailure "shared projection fixture must be an object",
      testCase "serializes an empty succession history as an empty array" $ do
        let response = liveResponseWith (Just (publicationWith Vector.empty))
        case publicationFrom (resolveLivePrForSliceResponseValue response) of
          Just (Object publication) ->
            assertEqual
              "empty succession history"
              (Just (toJSON ([] :: [Text])))
              (KeyMap.lookup "succession_invocation_ids" publication)
          _ -> assertFailure "publication evidence must be an object",
      testCase "keeps absent optional evidence absent rather than fabricating a record" $ do
        let response = liveResponseWith Nothing
        assertEqual
          "missing evidence is represented as null"
          (Just Null)
          (field "publication" (resolveLivePrForSliceResponseValue response)),
      testCase "preserves all resolution classifications and unknown enum handling" $ do
        let resolutions =
              [ (PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_LIVE, "live"),
                (PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_NEVER_PUBLISHED, "never_published"),
                (PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_ALL_ATTEMPTS_ABANDONED, "all_attempts_abandoned"),
                (PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_UNSPECIFIED, "unspecified")
              ]
        forM_ resolutions $ \(kind, expected) -> do
          let response =
                (liveResponseWith Nothing)
                  { PA.resolveLivePrForSliceResponseResolution = Protobuf.Enumerated (Right kind)
                  }
          assertEqual
            (T.unpack expected)
            (Just (toJSON expected))
            (field "resolution" (resolveLivePrForSliceResponseValue response))
        let unknown =
              (liveResponseWith Nothing)
                { PA.resolveLivePrForSliceResponseResolution = Protobuf.Enumerated (Left 99)
                }
        assertEqual
          "unknown enum"
          (Just (toJSON ("unspecified" :: Text)))
          (field "resolution" (resolveLivePrForSliceResponseValue unknown)),
      testCase "retains unsuccessful typed response handling" $ do
        let response =
              (liveResponseWith Nothing)
                { PA.resolveLivePrForSliceResponseSuccess = False,
                  PA.resolveLivePrForSliceResponseError = TL.pack "lookup rejected"
                }
        assertEqual
          "typed host failure"
          (Left "lookup rejected")
          (runCoreWith (encodedPayload response)),
      testCase "retains effect failure handling" $ do
        let failure =
              EffectError.EffectError
                ( Just
                    ( EffectError.EffectErrorKindCustom
                        (EffectError.Custom "lookup_failure" "host lookup unavailable" BS.empty)
                    )
                )
        assertEqual
          "effect failure"
          (Left "host lookup unavailable")
          (runCoreWith (encodedEnvelope (Envelope.EffectResponseResultError failure)))
    ]

findContractFixture :: IO FilePath
findContractFixture = do
  let candidates =
        [ "haskell/wasm-guest/test/fixtures/resolve_live_pr_for_slice_tool_result.json",
          "test/fixtures/resolve_live_pr_for_slice_tool_result.json",
          "contract-fixtures/resolve_live_pr_for_slice_tool_result.json"
        ]
  existing <- filterM doesFileExist candidates
  case existing of
    path : _ -> pure path
    [] -> assertFailure "shared Python contract fixture was not found"

contractSliceId :: Text
contractSliceId = "slice-contract-雪/\"stage"

contractPublication :: PA.PublishedHeadEvidence
contractPublication =
  publicationWith
    (Vector.fromList [TL.pack "invocation-original-1205", TL.pack "invocation-resumed-1205"])

liveResponse :: PA.ResolveLivePrForSliceResponse
liveResponse = liveResponseWith (Just contractPublication)

liveResponseWith :: Maybe PA.PublishedHeadEvidence -> PA.ResolveLivePrForSliceResponse
liveResponseWith publication =
  PA.ResolveLivePrForSliceResponse
    { PA.resolveLivePrForSliceResponseSuccess = True,
      PA.resolveLivePrForSliceResponseError = TL.pack "",
      PA.resolveLivePrForSliceResponseSliceId = TL.fromStrict contractSliceId,
      PA.resolveLivePrForSliceResponseResolution =
        Protobuf.Enumerated (Right PA.LivePrResolutionKindLIVE_PR_RESOLUTION_KIND_LIVE),
      PA.resolveLivePrForSliceResponsePrNumber = 46,
      PA.resolveLivePrForSliceResponsePublication = publication
    }

publicationWith :: Vector.Vector TL.Text -> PA.PublishedHeadEvidence
publicationWith succession =
  PA.PublishedHeadEvidence
    { PA.publishedHeadEvidenceInvocationId = TL.pack "invocation-1205-雪/\"head",
      PA.publishedHeadEvidenceSliceId = TL.fromStrict contractSliceId,
      PA.publishedHeadEvidenceAuthorAgent = TL.pack "stage-agent-雪/\"owner",
      PA.publishedHeadEvidenceSuccessionInvocationIds = succession
    }

assertPublication :: PA.PublishedHeadEvidence -> KeyMap.KeyMap Value -> IO ()
assertPublication expected result =
  case KeyMap.lookup "publication" result of
    Just (Object publication) -> do
      let actualKeys = sort (map Key.toText (KeyMap.keys publication))
          expectedKeys = sort ["author_agent", "invocation_id", "slice_id", "succession_invocation_ids"]
      assertEqual "Python consumer field names" expectedKeys actualKeys
      assertEqual "invocation_id" (Just (toJSON (TL.toStrict (PA.publishedHeadEvidenceInvocationId expected)))) (KeyMap.lookup "invocation_id" publication)
      assertEqual "slice_id" (Just (toJSON (TL.toStrict (PA.publishedHeadEvidenceSliceId expected)))) (KeyMap.lookup "slice_id" publication)
      assertEqual "author_agent" (Just (toJSON (TL.toStrict (PA.publishedHeadEvidenceAuthorAgent expected)))) (KeyMap.lookup "author_agent" publication)
      assertEqual
        "succession_invocation_ids"
        (Just (toJSON (map TL.toStrict (Vector.toList (PA.publishedHeadEvidenceSuccessionInvocationIds expected)))))
        (KeyMap.lookup "succession_invocation_ids" publication)
    _ -> assertFailure "publication evidence must be an object"

publicationFrom :: Value -> Maybe Value
publicationFrom = field "publication"

field :: Key.Key -> Value -> Maybe Value
field key (Object fields) = KeyMap.lookup key fields
field _ _ = Nothing

encodedPayload :: PA.ResolveLivePrForSliceResponse -> Value
encodedPayload response =
  encodedEnvelope
    ( Envelope.EffectResponseResultPayload
        (BL.toStrict (toLazyByteString response))
    )

encodedEnvelope :: Envelope.EffectResponseResult -> Value
encodedEnvelope result =
  toJSON
    ( BS.unpack
        ( BL.toStrict
            (toLazyByteString (Envelope.EffectResponse (Just result)))
        ) ::
        [Word8]
    )

runCoreWith :: Value -> Either Text Value
runCoreWith hostResponse =
  run $ runC (resolveLivePrForSliceCore (ResolveLivePrForSliceArgs contractSliceId)) >>= handle
  where
    handle (Done result) = pure result
    handle (Continue request resume)
      | erType request == "agent.resolve_live_pr_for_slice" =
          resume hostResponse >>= handle
      | otherwise = pure (Left "unexpected effect request")

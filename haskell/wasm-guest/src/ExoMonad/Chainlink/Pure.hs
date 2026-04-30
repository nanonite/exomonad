module ExoMonad.Chainlink.Pure
  ( -- * Issue Create
    ChainlinkIssueCreateArgs (..),
    buildCreateArgs,

    -- * Issue Show
    ChainlinkIssueShowOutput (..),
    buildShowArgs,

    -- * Issue Comment
    ChainlinkIssueCommentArgs (..),
    buildCommentArgs,
    ChainlinkIssueCommentOutput (..),

    -- * Subissue Create
    ChainlinkSubissueCreateArgs (..),
    buildSubissueArgs,

    -- * Session Work
    ChainlinkSessionWorkArgs (..),
    buildSessionWorkArgs,

    -- * Session End
    ChainlinkSessionEndArgs (..),
    buildSessionEndArgs,

    -- * Issue Close
    ChainlinkIssueCloseArgs (..),
    buildCloseArgs,

    -- * Utilities
    parseIssueId,
  )
where

import Data.Aeson (FromJSON (..), ToJSON (..), Value (Object), object, withObject, (.:), (.:?), (.!=), (.=))
import Data.Text (Text)
import Data.Text qualified as T
import GHC.Generics (Generic)

data ChainlinkIssueCreateArgs = ChainlinkIssueCreateArgs
  { cicaTitle :: Text,
    cicaDescription :: Maybe Text,
    cicaPriority :: Maybe Text,
    cicaLabels :: Maybe [Text]
  }
  deriving (Generic, Show)

data ChainlinkIssueShowOutput = ChainlinkIssueShowOutput
  { cisoId :: Int,
    cisoTitle :: Text,
    cisoStatus :: Text,
    cisoPriority :: Maybe Text,
    cisoLabels :: [Text]
  }
  deriving (Generic, Show, Eq)

instance FromJSON ChainlinkIssueShowOutput where
  parseJSON = withObject "ChainlinkIssueShowOutput" $ \v ->
    ChainlinkIssueShowOutput
      <$> v .: "id"
      <*> v .: "title"
      <*> v .: "status"
      <*> v .:? "priority"
      <*> v .:? "labels" .!= []

instance ToJSON ChainlinkIssueShowOutput where
  toJSON o =
    object
      [ "id" .= cisoId o,
        "title" .= cisoTitle o,
        "status" .= cisoStatus o,
        "priority" .= cisoPriority o,
        "labels" .= cisoLabels o
      ]

data ChainlinkIssueCommentArgs = ChainlinkIssueCommentArgs
  { cicIssueId :: Int,
    cicMessage :: Text
  }
  deriving (Generic, Show)

data ChainlinkIssueCommentOutput = ChainlinkIssueCommentOutput
  { cicoSuccess :: Bool
  }
  deriving (Generic, Show)

instance FromJSON ChainlinkIssueCommentOutput where
  parseJSON = withObject "ChainlinkIssueCommentOutput" $ \v ->
    ChainlinkIssueCommentOutput <$> v .: "success"

instance ToJSON ChainlinkIssueCommentOutput where
  toJSON o = object ["success" .= cicoSuccess o]

data ChainlinkSubissueCreateArgs = ChainlinkSubissueCreateArgs
  { cscParentId :: Int,
    cscTitle :: Text,
    cscPriority :: Maybe Text,
    cscLabels :: Maybe [Text]
  }
  deriving (Generic, Show)

data ChainlinkSessionWorkArgs = ChainlinkSessionWorkArgs
  { cswIssueId :: Int
  }
  deriving (Generic, Show)

data ChainlinkSessionEndArgs = ChainlinkSessionEndArgs
  { cseNotes :: Maybe Text
  }
  deriving (Generic, Show)

data ChainlinkIssueCloseArgs = ChainlinkIssueCloseArgs
  { cisIssueId :: Int
  }
  deriving (Generic, Show)

parseIssueId :: Text -> Maybe Int
parseIssueId output =
  case T.strip output of
    t
      | not (T.null t), T.all isDigit t -> Just (read (T.unpack t))
      | otherwise -> Nothing
  where
    isDigit c = c >= '0' && c <= '9'

buildCreateArgs :: ChainlinkIssueCreateArgs -> [String]
buildCreateArgs args =
  ["create", T.unpack (cicaTitle args), "-q"]
    ++ case cicaPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case cicaLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []

buildShowArgs :: Int -> [String]
buildShowArgs issueId = ["issue", "show", show issueId, "--json"]

buildCommentArgs :: ChainlinkIssueCommentArgs -> [String]
buildCommentArgs args =
  ["comment", show (cicIssueId args), T.unpack (cicMessage args)]

buildSubissueArgs :: ChainlinkSubissueCreateArgs -> [String]
buildSubissueArgs args =
  ["subissue", show (cscParentId args), T.unpack (cscTitle args)]
    ++ case cscPriority args of
      Just p -> ["-p", T.unpack p]
      Nothing -> []
    ++ case cscLabels args of
      Just labels -> concatMap (\l -> ["-l", T.unpack l]) labels
      Nothing -> []

buildSessionWorkArgs :: ChainlinkSessionWorkArgs -> [String]
buildSessionWorkArgs args = ["session", "work", show (cswIssueId args)]

buildSessionEndArgs :: ChainlinkSessionEndArgs -> [String]
buildSessionEndArgs args =
  ["session", "end"]
    ++ case cseNotes args of
      Just n -> ["--notes", T.unpack n]
      Nothing -> []

buildCloseArgs :: ChainlinkIssueCloseArgs -> [String]
buildCloseArgs args = ["close", show (cisIssueId args), "-q"]

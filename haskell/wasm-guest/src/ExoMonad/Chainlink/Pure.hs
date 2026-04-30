module ExoMonad.Chainlink.Pure
  ( parseIssueId,
    buildCreateArgs,
    ChainlinkIssueCreateArgs (..),
  )
where

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

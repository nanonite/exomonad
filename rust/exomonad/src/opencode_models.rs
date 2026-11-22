//! The committed OpenCode model table, keyed by harness version.
//!
//! `init` validates a configured OpenCode model against the table for the
//! version the installed CLI reports; it never asks the CLI for a catalog.
//! `opencode models` output depends on a background service and is not a stable
//! input (Chainlink #1358), so the supported models and their effort variants
//! are committed next to the code and embedded at compile time.
//!
//! Adding a harness version is one table file, one entry in
//! [`EMBEDDED_OPENCODE_TABLES`], and a test — never a code branch per version.

use anyhow::{bail, Context, Result};
use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::OnceLock;

/// The only table schema this build understands.
pub const TABLE_SCHEMA_VERSION: u32 = 1;

/// The harness a table describes. The version gate is per harness binary, so a
/// table that names another harness is malformed rather than merely unused.
pub const TABLE_HARNESS: &str = "opencode";

/// Repository-relative directory the tables live in. Errors name the file with
/// this prefix so the reader can open it.
pub const TABLE_DIRECTORY: &str = "rust/exomonad/data/opencode";

/// Every committed OpenCode table, keyed by the harness version it records.
///
/// The key is compared with the normalized version token from
/// `opencode --version` (an optional leading `v` is accepted). One table per
/// key; [`parse_table`] refuses a table whose recorded `version` differs from
/// the key it is registered under.
pub const EMBEDDED_OPENCODE_TABLES: &[(&str, &str)] = &[
    ("v2.0.22", include_str!("../data/opencode/v2.0.22.json")),
    ("v2.3.2", include_str!("../data/opencode/v2.3.2.json")),
];

/// An effort variant OpenCode accepts as `--variant`.
///
/// The vocabulary is closed: a table that names anything else fails to parse,
/// so a typo cannot silently widen what validation accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OpencodeEffort {
    None,
    Minimal,
    Low,
    Medium,
    High,
    Xhigh,
    Max,
}

impl OpencodeEffort {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Minimal => "minimal",
            Self::Low => "low",
            Self::Medium => "medium",
            Self::High => "high",
            Self::Xhigh => "xhigh",
            Self::Max => "max",
        }
    }
}

impl fmt::Display for OpencodeEffort {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// One model's recorded effort variants.
///
/// `Some([])` means the model has no effort variants; `None` means the variants
/// are not recorded yet. The two are distinct on purpose: the first refuses
/// every effort because the model supports none, the second refuses every
/// effort until the table records them. The `efforts` key is mandatory — an
/// entry without it is malformed rather than silently unrecorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpencodeModelEntry {
    pub efforts: Option<Vec<OpencodeEffort>>,
}

impl<'de> Deserialize<'de> for OpencodeModelEntry {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct RawEntry {
            efforts: EffortsField,
        }

        #[derive(Deserialize)]
        #[serde(untagged)]
        enum EffortsField {
            Recorded(Vec<OpencodeEffort>),
            Unrecorded(()),
        }

        let raw = RawEntry::deserialize(deserializer)?;
        Ok(Self {
            efforts: match raw.efforts {
                EffortsField::Recorded(efforts) => Some(efforts),
                EffortsField::Unrecorded(()) => None,
            },
        })
    }
}

/// A parsed, validated table for one harness version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpencodeModelTable {
    pub version: String,
    pub models: BTreeMap<String, OpencodeModelEntry>,
}

impl OpencodeModelTable {
    pub fn file(&self) -> String {
        table_path(&self.version)
    }

    pub fn model(&self, model: &str) -> Option<&OpencodeModelEntry> {
        self.models.get(model)
    }
}

/// Repository-relative path of the table for `version`.
pub fn table_path(version: &str) -> String {
    format!("{TABLE_DIRECTORY}/{version}.json")
}

/// The models object of a table.
///
/// Deserialized through a visitor rather than `BTreeMap` so a duplicate key is
/// an error instead of a silent last-wins overwrite.
struct OpencodeModels(BTreeMap<String, OpencodeModelEntry>);

impl<'de> Deserialize<'de> for OpencodeModels {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct ModelsVisitor;

        impl<'de> Visitor<'de> for ModelsVisitor {
            type Value = OpencodeModels;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a map of `provider/model` ids to model entries")
            }

            fn visit_map<A>(self, mut map: A) -> std::result::Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut models = BTreeMap::new();
                while let Some((id, entry)) = map.next_entry::<String, OpencodeModelEntry>()? {
                    if models.insert(id.clone(), entry).is_some() {
                        return Err(de::Error::custom(format!(
                            "duplicate model id `{id}` in the models map"
                        )));
                    }
                }
                Ok(OpencodeModels(models))
            }
        }

        deserializer.deserialize_map(ModelsVisitor)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawTable {
    schema_version: u32,
    harness: String,
    version: String,
    provenance: String,
    models: OpencodeModels,
}

/// Parse and validate one embedded table.
///
/// `version_key` is the key the table is registered under in
/// [`EMBEDDED_OPENCODE_TABLES`]; a table that records another version is a
/// registration bug and fails here. Every error names the table file.
pub fn parse_table(version_key: &str, json: &str) -> Result<OpencodeModelTable> {
    let file = table_path(version_key);
    let raw: RawTable = serde_json::from_str(json)
        .with_context(|| format!("OpenCode model table `{file}` is malformed"))?;

    if raw.schema_version != TABLE_SCHEMA_VERSION {
        bail!(
            "OpenCode model table `{file}` has schema_version {}; this build understands only {TABLE_SCHEMA_VERSION}",
            raw.schema_version
        );
    }
    if raw.harness != TABLE_HARNESS {
        bail!(
            "OpenCode model table `{file}` describes harness `{}`; expected `{TABLE_HARNESS}`",
            raw.harness
        );
    }
    if raw.version != version_key {
        bail!(
            "OpenCode model table `{file}` records version `{}` but is registered under `{version_key}`; the registry key and the table version must match",
            raw.version
        );
    }
    if raw.provenance.trim().is_empty() {
        bail!("OpenCode model table `{file}` has an empty provenance string");
    }
    if raw.models.0.is_empty() {
        bail!("OpenCode model table `{file}` lists no models");
    }

    for (id, entry) in &raw.models.0 {
        validate_model_id(id)
            .with_context(|| format!("OpenCode model table `{file}` has an invalid model id"))?;
        if let Some(efforts) = &entry.efforts {
            let mut seen = BTreeSet::new();
            for effort in efforts {
                if !seen.insert(*effort) {
                    bail!(
                        "OpenCode model table `{file}` lists effort `{effort}` twice for model `{id}`"
                    );
                }
            }
        }
    }

    Ok(OpencodeModelTable {
        version: raw.version,
        models: raw.models.0,
    })
}

/// A table model id is exactly one `provider/model` pair.
fn validate_model_id(id: &str) -> Result<()> {
    let Some((provider, model)) = id.split_once('/') else {
        bail!("`{id}` is not of the form `provider/model`");
    };
    if provider.is_empty() || model.is_empty() {
        bail!("`{id}` is not of the form `provider/model`");
    }
    if model.contains('/') {
        bail!("`{id}` contains more than one `/`");
    }
    Ok(())
}

/// Parse every embedded table once.
pub fn tables() -> Result<&'static [OpencodeModelTable]> {
    static TABLES: OnceLock<std::result::Result<Vec<OpencodeModelTable>, String>> = OnceLock::new();
    let tables = TABLES.get_or_init(|| {
        EMBEDDED_OPENCODE_TABLES
            .iter()
            .map(|(version, json)| parse_table(version, json).map_err(|error| format!("{error:#}")))
            .collect()
    });
    match tables {
        Ok(tables) => Ok(tables.as_slice()),
        Err(message) => bail!("{message}"),
    }
}

/// The table for a normalized harness version, if one is committed.
pub fn table_for_version(version: &str) -> Result<Option<&'static OpencodeModelTable>> {
    Ok(tables()?.iter().find(|table| table.version == version))
}

/// The committed versions, in registry order, for an unknown-version error.
pub fn known_versions() -> Result<Vec<&'static str>> {
    Ok(tables()?
        .iter()
        .map(|table| table.version.as_str())
        .collect())
}

/// Extract the version token from `opencode --version` output.
///
/// Accepts `opencode v2.0.22`, `2.0.22`, and a bare version token among other
/// words. Returns the normalized `v`-prefixed form the table registry uses, or
/// `None` when the output holds no recognizable version.
pub fn parse_version_token(output: &str) -> Option<String> {
    output.split_whitespace().find_map(|token| {
        let core = token.strip_prefix('v').unwrap_or(token);
        if !is_version_core(core) {
            return None;
        }
        Some(format!("v{core}"))
    })
}

fn is_version_core(core: &str) -> bool {
    core.contains('.')
        && core
            .split('.')
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

/// Refuse or accept one model/effort pair against a parsed table.
///
/// This is the single rule every role route obeys: a known version, a listed
/// model, and (when requested) an effort the model records.
pub fn validate_model(table: &OpencodeModelTable, model: &str, effort: Option<&str>) -> Result<()> {
    let file = table.file();
    let Some(entry) = table.model(model) else {
        bail!(
            "Unknown OpenCode model `{model}` for harness version `{}`. The static table `{file}` does not list it. Add the model (and its effort variants) to that table and to the matching test.",
            table.version
        );
    };

    let Some(effort) = effort.filter(|value| !value.is_empty()) else {
        return Ok(());
    };

    let Some(efforts) = &entry.efforts else {
        bail!(
            "OpenCode effort `{effort}` cannot be validated for model `{model}` in harness version `{}`: `{file}` does not record effort variants for this model. Record them under models[\"{model}\"].efforts (or `[]` if the model supports none) before requesting an effort.",
            table.version
        );
    };

    if efforts.is_empty() {
        bail!(
            "OpenCode effort `{effort}` is not supported by model `{model}` in harness version `{}`: `{file}` records no effort variants for it. Remove the requested effort.",
            table.version
        );
    }

    if !efforts.iter().any(|supported| supported.as_str() == effort) {
        let supported = efforts
            .iter()
            .map(|supported| supported.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        bail!(
            "Unsupported OpenCode effort `{effort}` for model `{model}` in harness version `{}`. Supported efforts in `{file}`: {supported}. Correct with the role-specific effort flag.",
            table.version
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shipped_table() -> &'static OpencodeModelTable {
        tables()
            .expect("the shipped OpenCode table parses")
            .first()
            .expect("at least one table is embedded")
    }

    #[test]
    fn shipped_table_parses_with_seventy_three_unique_models() {
        let table = shipped_table();
        assert_eq!(table.version, "v2.0.22");
        assert_eq!(table.models.len(), 73);
        let ids = table.models.keys().collect::<BTreeSet<_>>();
        assert_eq!(ids.len(), table.models.len(), "model ids must be unique");
    }

    #[test]
    fn every_embedded_table_parses_and_matches_its_registry_key() {
        let tables = tables().expect("every embedded table parses");
        assert_eq!(tables.len(), EMBEDDED_OPENCODE_TABLES.len());
        for (table, (key, _)) in tables.iter().zip(EMBEDDED_OPENCODE_TABLES) {
            assert_eq!(&table.version, key);
        }
    }

    #[test]
    fn table_ids_equal_the_committed_models_fixture() {
        let fixture = include_str!("../data/opencode/v2.0.22.models.txt");
        let fixture_ids = fixture
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .collect::<BTreeSet<_>>();
        let table = shipped_table();
        let table_ids = table
            .models
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        assert_eq!(
            table_ids, fixture_ids,
            "the committed table and `opencode models` fixture must list the same models"
        );
    }

    fn table_for(version: &str) -> &'static OpencodeModelTable {
        tables()
            .expect("the shipped OpenCode tables parse")
            .iter()
            .find(|table| table.version == version)
            .unwrap_or_else(|| panic!("{version} is committed"))
    }

    #[test]
    fn v232_table_parses_with_fifty_nine_unique_models() {
        let table = table_for("v2.3.2");
        assert_eq!(table.version, "v2.3.2");
        assert_eq!(table.models.len(), 59);
        let ids = table.models.keys().collect::<BTreeSet<_>>();
        assert_eq!(ids.len(), table.models.len(), "model ids must be unique");
    }

    #[test]
    fn v232_table_ids_equal_the_committed_models_fixture() {
        let fixture = include_str!("../data/opencode/v2.3.2.models.txt");
        let fixture_ids = fixture
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .collect::<BTreeSet<_>>();
        let table = table_for("v2.3.2");
        let table_ids = table
            .models
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        assert_eq!(
            table_ids, fixture_ids,
            "the committed v2.3.2 table and `opencode models` fixture must list the same models"
        );
    }

    #[test]
    fn v232_policy_routes_are_in_the_table_with_their_recorded_efforts() {
        let table = table_for("v2.3.2");
        for effort in ["low", "high", "max"] {
            validate_model(table, "opencode-go/deepseek-v4.1-flash", Some(effort)).unwrap_or_else(
                |error| panic!("deepseek-v4.1-flash must accept effort `{effort}`: {error:#}"),
            );
        }
        validate_model(table, "opencode-go/glm-5.3-flash", None).expect("glm-5.3-flash is listed");
        validate_model(table, "opencode-go/glm-5.3-flash", Some("high"))
            .expect("glm-5.3-flash records the high effort");
        validate_model(table, "opencode-go/deepseek-v4-pro", None)
            .expect("deepseek-v4-pro is listed");
        validate_model(table, "opencode-go/deepseek-v4-pro", Some("high"))
            .expect("deepseek-v4-pro records the high effort");
        validate_model(table, "opencode-go/deepseek-v4-pro", Some("max"))
            .expect("deepseek-v4-pro records the max effort");
    }

    #[test]
    fn parse_version_token_accepts_optional_leading_v() {
        assert_eq!(
            parse_version_token("opencode v2.0.22").as_deref(),
            Some("v2.0.22")
        );
        assert_eq!(
            parse_version_token("opencode 2.0.22\n").as_deref(),
            Some("v2.0.22")
        );
        assert_eq!(
            parse_version_token("OpenCode CLI version v9.9.9 (build abc)").as_deref(),
            Some("v9.9.9")
        );
    }

    #[test]
    fn parse_version_token_refuses_output_without_a_version() {
        assert_eq!(parse_version_token("opencode"), None);
        assert_eq!(parse_version_token(""), None);
        assert_eq!(parse_version_token("version unknown"), None);
        assert_eq!(parse_version_token("v..1"), None);
    }

    #[test]
    fn malformed_table_names_the_file_and_the_reason() {
        let base = r#"{"schema_version":1,"harness":"opencode","version":"v2.0.22","provenance":"captured","models":{"p/m":{"efforts":null}}}"#;
        assert!(parse_table("v2.0.22", base).is_ok());

        let wrong_schema = base.replace("\"schema_version\":1", "\"schema_version\":2");
        let error = format!("{:#}", parse_table("v2.0.22", &wrong_schema).unwrap_err());
        assert!(error.contains("rust/exomonad/data/opencode/v2.0.22.json"));
        assert!(error.contains("schema_version"));

        let wrong_harness = base.replace("\"harness\":\"opencode\"", "\"harness\":\"codex\"");
        let error = format!("{:#}", parse_table("v2.0.22", &wrong_harness).unwrap_err());
        assert!(error.contains("harness `codex`"));

        let wrong_version = base.replace("\"version\":\"v2.0.22\"", "\"version\":\"v2.0.21\"");
        let error = format!("{:#}", parse_table("v2.0.22", &wrong_version).unwrap_err());
        assert!(error.contains("registered under `v2.0.22`"));

        let bad_id = base.replace("\"p/m\"", "\"no-provider\"");
        let error = format!("{:#}", parse_table("v2.0.22", &bad_id).unwrap_err());
        assert!(error.contains("provider/model"));

        let bad_effort = base.replace("\"efforts\":null", "\"efforts\":[\"extreme\"]");
        let error = format!("{:#}", parse_table("v2.0.22", &bad_effort).unwrap_err());
        assert!(error.contains("rust/exomonad/data/opencode/v2.0.22.json"));
    }

    #[test]
    fn table_rejects_duplicate_model_ids() {
        let json = r#"{"schema_version":1,"harness":"opencode","version":"v2.0.22","provenance":"captured","models":{"p/m":{"efforts":null},"p/m":{"efforts":[]}}}"#;
        let error = format!("{:#}", parse_table("v2.0.22", json).unwrap_err());
        assert!(error.contains("duplicate model id `p/m`"));
    }

    #[test]
    fn table_rejects_a_missing_efforts_key() {
        let json = r#"{"schema_version":1,"harness":"opencode","version":"v2.0.22","provenance":"captured","models":{"p/m":{}}}"#;
        assert!(parse_table("v2.0.22", json).is_err());
    }

    #[test]
    fn known_model_without_effort_passes() {
        let table = shipped_table();
        assert!(validate_model(table, "opencode-go/deepseek-v4.1-flash", None).is_ok());
        assert!(validate_model(table, "opencode-go/deepseek-v4.1-flash", Some("")).is_ok());
    }

    #[test]
    fn known_model_with_recorded_effort_passes() {
        let table = shipped_table();
        assert!(validate_model(table, "opencode-go/deepseek-v4.1-flash", Some("high")).is_ok());
        assert!(validate_model(table, "opencode-go/glm-5.3-flash", Some("high")).is_ok());
    }

    #[test]
    fn unknown_model_names_the_model_version_and_table_file() {
        let error = validate_model(shipped_table(), "opencode-go/not-a-model", None)
            .unwrap_err()
            .to_string();
        assert!(error.contains("Unknown OpenCode model `opencode-go/not-a-model`"));
        assert!(error.contains("v2.0.22"));
        assert!(error.contains("rust/exomonad/data/opencode/v2.0.22.json"));
    }

    #[test]
    fn unrecorded_efforts_are_refused_rather_than_passed_through() {
        // `opencode-go/beam-free` records `efforts: null`.
        let error = validate_model(shipped_table(), "opencode-go/beam-free", Some("high"))
            .unwrap_err()
            .to_string();
        assert!(error.contains("does not record effort variants"));
        assert!(error.contains("opencode-go/beam-free"));
        assert!(error.contains("rust/exomonad/data/opencode/v2.0.22.json"));
    }

    #[test]
    fn model_with_no_effort_variants_refuses_every_effort() {
        // `opencode-go/kimi-k2.7-code` records `efforts: []`.
        let error = validate_model(shipped_table(), "opencode-go/kimi-k2.7-code", Some("high"))
            .unwrap_err()
            .to_string();
        assert!(error.contains("records no effort variants"));
        assert!(error.contains("opencode-go/kimi-k2.7-code"));
    }

    #[test]
    fn unsupported_effort_lists_the_supported_ones() {
        let error = validate_model(
            shipped_table(),
            "opencode-go/deepseek-v4.1-flash",
            Some("minimal"),
        )
        .unwrap_err()
        .to_string();
        assert!(error.contains("Unsupported OpenCode effort `minimal`"));
        assert!(error.contains("low, high, max"));
        assert!(error.contains("rust/exomonad/data/opencode/v2.0.22.json"));
    }
}

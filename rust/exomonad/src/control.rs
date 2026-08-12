//! Authentication boundary for the operator control route group.

use axum::{
    extract::State,
    http::{header::HeaderName, HeaderMap, Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};
use std::sync::Arc;

pub const CONTROL_CREDENTIAL_ENV: &str = "EXOMONAD_CONTROL_TOKEN";
pub const AGENT_CREDENTIAL_ENV: &str = "EXOMONAD_AGENT_TOKEN";
pub const CONTROL_CREDENTIAL_HEADER: HeaderName =
    HeaderName::from_static("x-exomonad-control-credential");
pub const AGENT_CREDENTIAL_HEADER: HeaderName =
    HeaderName::from_static("x-exomonad-agent-credential");

#[derive(Clone, Default)]
pub struct RouteAuth {
    control_credential: Option<Arc<str>>,
    agent_credential: Option<Arc<str>>,
}

impl RouteAuth {
    pub fn from_env() -> Self {
        Self {
            control_credential: credential_from_env(CONTROL_CREDENTIAL_ENV),
            agent_credential: credential_from_env(AGENT_CREDENTIAL_ENV),
        }
    }

    #[cfg(test)]
    fn with_credentials(control: Option<&str>, agent: Option<&str>) -> Self {
        Self {
            control_credential: control.map(Arc::<str>::from),
            agent_credential: agent.map(Arc::<str>::from),
        }
    }

    fn control_request_authorized(&self, headers: &HeaderMap) -> bool {
        if headers.contains_key(AGENT_CREDENTIAL_HEADER) {
            return false;
        }
        credential_matches(
            headers,
            &CONTROL_CREDENTIAL_HEADER,
            self.control_credential.as_deref(),
        )
    }

    pub fn agent_request_authorized(&self, headers: &HeaderMap) -> bool {
        if headers.contains_key(CONTROL_CREDENTIAL_HEADER) {
            return false;
        }
        match self.agent_credential.as_deref() {
            Some(expected) => credential_matches(headers, &AGENT_CREDENTIAL_HEADER, Some(expected)),
            None => !headers.contains_key(AGENT_CREDENTIAL_HEADER),
        }
    }
}

pub async fn require_control(
    State(auth): State<RouteAuth>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    if !auth.control_request_authorized(request.headers()) {
        return unauthorized_response();
    }
    next.run(request).await
}

pub fn unauthorized_response() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        [(axum::http::header::WWW_AUTHENTICATE, "ExoMonad-Control")],
        "control credential required",
    )
        .into_response()
}

fn credential_from_env(name: &str) -> Option<Arc<str>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(Arc::<str>::from)
}

fn credential_matches(headers: &HeaderMap, header: &HeaderName, expected: Option<&str>) -> bool {
    let Some(expected) = expected else {
        return false;
    };
    headers.get(header).and_then(|value| value.to_str().ok()) == Some(expected)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_and_agent_credentials_are_disjoint() {
        let auth = RouteAuth::with_credentials(Some("control-secret"), Some("agent-secret"));
        let mut control = HeaderMap::new();
        control.insert(CONTROL_CREDENTIAL_HEADER, "control-secret".parse().unwrap());
        let mut agent = HeaderMap::new();
        agent.insert(AGENT_CREDENTIAL_HEADER, "agent-secret".parse().unwrap());

        assert!(auth.control_request_authorized(&control));
        assert!(!auth.agent_request_authorized(&control));
        assert!(auth.agent_request_authorized(&agent));
        assert!(!auth.control_request_authorized(&agent));
    }

    #[test]
    fn missing_or_wrong_control_credentials_fail_closed() {
        let auth = RouteAuth::with_credentials(Some("control-secret"), None);
        assert!(!auth.control_request_authorized(&HeaderMap::new()));

        let mut wrong = HeaderMap::new();
        wrong.insert(CONTROL_CREDENTIAL_HEADER, "agent-secret".parse().unwrap());
        assert!(!auth.control_request_authorized(&wrong));
    }

    #[test]
    fn legacy_agent_routes_remain_socket_authenticated_when_unconfigured() {
        let auth = RouteAuth::with_credentials(None, None);
        assert!(auth.agent_request_authorized(&HeaderMap::new()));
    }

    #[test]
    fn configured_agent_credential_is_required_for_agent_routes() {
        let auth = RouteAuth::with_credentials(None, Some("agent-secret"));
        let mut headers = HeaderMap::new();
        headers.insert(AGENT_CREDENTIAL_HEADER, "agent-secret".parse().unwrap());
        assert!(auth.agent_request_authorized(&headers));

        headers.insert(AGENT_CREDENTIAL_HEADER, "control-secret".parse().unwrap());
        assert!(!auth.agent_request_authorized(&headers));
    }
}

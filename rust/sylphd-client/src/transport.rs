//! Blocking `reqwest` transport for a Unix socket or an HTTP(S) base URL.
//!
//! Sends `User-Agent: sylphd/<version>`, the bearer
//! token, `x-token-valid` warnings, and retries of `429`/`503` honouring
//! `Retry-After`.

use std::time::Duration;

use bytes::Bytes;
use reqwest::blocking::multipart::{Form, Part};
use reqwest::blocking::{Client as HttpClient, ClientBuilder, RequestBuilder};
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE};
use serde_json::Value;
use sylphd_core::document::{InfoDocument, MessageItem};

use crate::address::{Address, AddressArgs, Env};
use crate::PROG;

/// `Content-Type` of an `/assembly` body that is a sketch.
pub const SKETCH_CONTENT_TYPE: &str = "application/vnd.sylph.sketch";

/// Statuses that mean "try again shortly".
const RETRY_STATUSES: [u16; 2] = [429, 503];

/// Longest wait between two attempts, in seconds.
pub const MAX_RETRY_DELAY: f64 = 30.0;

/// Seconds `GET /messages` may take.
pub const MESSAGES_TIMEOUT: f64 = 3.0;

const CONNECT_TIMEOUT_SECONDS: u64 = 30;

/// A client failure (connection or HTTP error).
#[derive(Debug, Clone)]
pub struct ClientError(pub String);

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ClientError {}

/// Installs the `ring` provider once; `rustls-no-provider` requires it before
/// the first client is built.
pub fn init_crypto() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

/// A retry delay for the next attempt: `Retry-After`, but at least
/// `1, 2, 4...` seconds, capped at [`MAX_RETRY_DELAY`] .
pub fn retry_delay(retry_after: Option<&str>, attempt: u32) -> f64 {
    let advised = retry_after
        .and_then(|text| text.trim().parse::<f64>().ok())
        .unwrap_or(0.0);
    let backoff = 2f64.powi(attempt as i32);
    MAX_RETRY_DELAY.min(advised.max(backoff))
}

/// A connected client for one command invocation.
pub struct Client {
    inner: HttpClient,
    address: Address,
    base: String,
    /// `None` while nothing is known or no token was sent.
    token_valid: Option<bool>,
    retries: u32,
}

impl Client {
    /// Builds the transport for a resolved address.
    pub fn new(
        address: Address,
        timeout: Option<f64>,
        retries: u32,
        token: Option<String>,
    ) -> Result<Self, ClientError> {
        init_crypto();
        let mut builder = ClientBuilder::new()
            .user_agent(format!("sylphd/{}", env!("CARGO_PKG_VERSION")))
            .gzip(true)
            .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECONDS))
            .redirect(reqwest::redirect::Policy::none());
        builder = match timeout {
            Some(seconds) => builder.timeout(Duration::from_secs_f64(seconds)),
            None => builder.timeout(None),
        };

        let base = match &address {
            Address::Socket(path) => {
                #[cfg(unix)]
                {
                    builder = builder.unix_socket(path.as_path());
                }
                #[cfg(not(unix))]
                {
                    let _ = path;
                    return Err(ClientError(
                        "Unix sockets are not supported on this platform; use --url or --tcp"
                            .into(),
                    ));
                }
                "http://sylphd.local".to_string()
            }
            Address::Url(url) => {
                let parsed = reqwest::Url::parse(url)
                    .map_err(|e| ClientError(format!("invalid server URL {url:?}: {e}")))?;
                if parsed.scheme() == "http"
                    && token.is_some()
                    && !crate::address::is_local_host(&parsed)
                {
                    return Err(ClientError(
                        "refusing to send a token over plain http".into(),
                    ));
                }
                url.clone()
            }
        };

        if let Some(token) = &token {
            let mut headers = HeaderMap::new();
            let value = HeaderValue::from_str(&format!("Bearer {token}"))
                .map_err(|_| ClientError("invalid access token (not a header value)".into()))?;
            headers.insert(AUTHORIZATION, value);
            builder = builder.default_headers(headers);
        }

        let inner = builder
            .build()
            .map_err(|e| ClientError(format!("cannot set up the HTTP client: {e}")))?;
        Ok(Self {
            inner,
            address,
            base,
            token_valid: None,
            retries,
        })
    }

    /// The base URL, when the client talks HTTP(S) .
    pub fn base_url(&self) -> Option<&str> {
        match &self.address {
            Address::Url(_) => Some(&self.base),
            Address::Socket(_) => None,
        }
    }

    pub fn address(&self) -> &Address {
        &self.address
    }

    fn url(&self, path: &str) -> String {
        format!("{}/{}", self.base, path.trim_start_matches('/'))
    }

    fn connection_error(&self, error: reqwest::Error) -> ClientError {
        ClientError(format!(
            "cannot talk to sylphd {}: {error}",
            self.address.describe()
        ))
    }

    fn note_token(&mut self, headers: &HeaderMap) {
        let Some(verdict) = headers.get("x-token-valid") else {
            return;
        };
        let valid = verdict
            .to_str()
            .map(|text| text.trim().eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        let was = self.token_valid;
        self.token_valid = Some(valid);
        if !valid && was != Some(false) {
            let where_str = match &self.address {
                Address::Url(url) => url.clone(),
                Address::Socket(path) => format!("the server on {}", path.display()),
            };
            eprintln!(
                "{PROG}: warning: {where_str} does not accept this access token \
                 (mistyped, or replaced on the server?); you are treated as an \
                 anonymous client"
            );
        }
    }

    /// Runs `make` until the answer is not a retryable status, then checks the
    /// status and returns the body.
    fn send_with_retry(
        &mut self,
        mut make: impl FnMut() -> Result<RequestBuilder, ClientError>,
    ) -> Result<String, ClientError> {
        let mut attempt = 0u32;
        loop {
            let request = make()?;
            let response = request
                .send()
                .map_err(|error| self.connection_error(error))?;
            let status = response.status().as_u16();
            let reason = response
                .status()
                .canonical_reason()
                .unwrap_or("")
                .to_string();
            let retry = RETRY_STATUSES.contains(&status) && attempt < self.retries;
            if !retry {
                self.note_token(response.headers());
                if response.status().as_u16() >= 400 {
                    let body = response.text().unwrap_or_default();
                    return Err(ClientError(response_error_text(status, &reason, body)));
                }
                return response
                    .text()
                    .map_err(|error| self.connection_error(error));
            }
            let delay = retry_delay(
                response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok()),
                attempt,
            );
            attempt += 1;
            std::thread::sleep(Duration::from_secs_f64(delay));
        }
    }

    pub fn get_info(&mut self) -> Result<(String, InfoDocument), ClientError> {
        let url = self.url("info");
        let inner = self.inner.clone();
        let raw = self.send_with_retry(move || Ok(inner.get(&url)))?;
        let info: InfoDocument = sylphd_core::document::parse(&raw).map_err(|e| ClientError(e.to_string()))?;
        Ok((raw, info))
    }

    /// `GET /messages`, best effort: an error or an odd document is `[]`.
    pub fn messages(&self) -> Vec<MessageItem> {
        let url = self.url("messages");
        let response = self
            .inner
            .get(url)
            .timeout(Duration::from_secs_f64(MESSAGES_TIMEOUT))
            .send();
        let Ok(response) = response else {
            return Vec::new();
        };
        if response.status().as_u16() != 200 {
            return Vec::new();
        }
        let Ok(text) = response.text() else {
            return Vec::new();
        };
        let Ok(value) = serde_json::from_str::<Value>(&text) else {
            return Vec::new();
        };
        let Some(items) = value.get("messages").and_then(Value::as_array) else {
            return Vec::new();
        };
        items
            .iter()
            .filter_map(|item| serde_json::from_value::<MessageItem>(item.clone()).ok())
            .filter(|item| matches!(item.level.as_str(), "info" | "warning" | "danger"))
            .collect()
    }

    /// `POST /profile` or `POST /query`: one `sketch` part per sample plus a
    /// `params` JSON field.
    pub fn post_sketches(
        &mut self,
        endpoint: &str,
        params_json: &str,
        parts: &[(String, Bytes)],
    ) -> Result<String, ClientError> {
        let url = self.url(endpoint);
        let inner = self.inner.clone();
        let params_json = params_json.to_string();
        let parts = parts.to_vec(); // `Bytes` clones share the sketch
        self.send_with_retry(move || {
            let mut form = Form::new().text("params", params_json.clone());
            for (name, bytes) in &parts {
                form = form.part(
                    "sketch",
                    Part::reader_with_length(std::io::Cursor::new(bytes.clone()), bytes.len() as u64)
                        .file_name(format!("{name}.sylsp"))
                        .mime_str("application/octet-stream")
                        .map_err(|e| ClientError(format!("multipart error: {e}")))?,
                );
            }
            Ok(inner.post(&url).multipart(form))
        })
    }

    /// `POST /assembly` with a sketch or raw sequence body and query parameters.
    pub fn post_assembly(
        &mut self,
        query: &[(String, String)],
        body: Bytes,
        content_type: &str,
    ) -> Result<String, ClientError> {
        let url = self.url("assembly");
        let inner = self.inner.clone();
        let query = query.to_vec();
        let content_type = content_type.to_string();
        self.send_with_retry(move || {
            let mut request = inner.post(&url).query(&query).body(body.clone());
            request = request.header(CONTENT_TYPE, content_type.clone());
            Ok(request)
        })
    }

    /// Builds the transport from CLI options .
    pub fn from_cli(
        args: &AddressArgs,
        env: &dyn Env,
        timeout: Option<f64>,
        retries: u32,
        token: Option<String>,
    ) -> Result<Self, ClientError> {
        let address = crate::address::resolve_address(args, env).map_err(|e| ClientError(e.0))?;
        Self::new(address, timeout, retries, token)
    }
}

fn response_error_text(status: u16, reason: &str, body: String) -> String {
    let parsed: Option<Value> = serde_json::from_str(&body).ok();
    let message = parsed
        .as_ref()
        .and_then(|value| value.get("error"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            let text = body.trim();
            if text.is_empty() {
                (!reason.is_empty()).then_some(reason.to_string())
            } else {
                Some(text.to_string())
            }
        })
        .unwrap_or_default();
    format!("server error ({status}): {message}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_delay_honours_retry_after_and_backoff() {
        assert_eq!(retry_delay(None, 0), 1.0);
        assert_eq!(retry_delay(None, 1), 2.0);
        assert_eq!(retry_delay(None, 4), 16.0);
        assert_eq!(retry_delay(Some("5"), 0), 5.0);
        assert_eq!(retry_delay(Some("0"), 2), 4.0);
        assert_eq!(retry_delay(Some("40"), 0), MAX_RETRY_DELAY);
        assert_eq!(retry_delay(Some("not a date"), 1), 2.0);
    }

    #[test]
    fn error_body_prefers_the_json_error_field() {
        assert_eq!(
            response_error_text(400, "", r#"{"error":"bad sketch"}"#.into()),
            "server error (400): bad sketch"
        );
        assert_eq!(
            response_error_text(500, "Internal Server Error", "  boom \n".into()),
            "server error (500): boom"
        );
        assert_eq!(
            response_error_text(503, "Service Unavailable", "   ".into()),
            "server error (503): Service Unavailable"
        );
    }

    /// A one-connection-per-response HTTP stub: answers the scripted raw
    /// responses in order, one per accepted connection, and counts requests.
    fn stub(responses: Vec<String>) -> (String, std::thread::JoinHandle<usize>) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = std::thread::spawn(move || {
            let mut served = 0;
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buffer = [0u8; 4096];
                let mut seen = Vec::new();
                while !seen.windows(4).any(|w| w == b"\r\n\r\n") {
                    let n = stream.read(&mut buffer).unwrap();
                    if n == 0 {
                        break;
                    }
                    seen.extend_from_slice(&buffer[..n]);
                }
                stream.write_all(response.as_bytes()).unwrap();
                served += 1;
            }
            served
        });
        (base, handle)
    }

    fn reply(status: &str, headers: &str, body: &str) -> String {
        format!(
            "HTTP/1.1 {status}\r\ncontent-length: {}\r\nconnection: close\r\n{headers}\r\n{body}",
            body.len()
        )
    }

    fn get(client: &mut Client, path: &str) -> Result<String, ClientError> {
        let url = client.url(path);
        let inner = client.inner.clone();
        client.send_with_retry(move || Ok(inner.get(&url)))
    }

    #[test]
    fn retries_429_honouring_retry_after_then_succeeds() {
        let (base, server) = stub(vec![
            reply("429 Too Many Requests", "retry-after: 0\r\n", "slow down"),
            reply("200 OK", "", "{\"ok\":true}"),
        ]);
        let mut client = Client::new(Address::Url(base), None, 2, None).unwrap();
        let started = std::time::Instant::now();
        assert_eq!(get(&mut client, "info").unwrap(), "{\"ok\":true}");
        // `Retry-After: 0` still waits the 1 s backoff of the first attempt.
        assert!(started.elapsed() >= Duration::from_secs(1));
        assert_eq!(server.join().unwrap(), 2);
    }

    #[test]
    fn exhausted_retries_report_the_server_error() {
        let (base, server) = stub(vec![
            reply("503 Service Unavailable", "retry-after: 0\r\n", "{\"error\":\"busy\"}"),
            reply("503 Service Unavailable", "retry-after: 0\r\n", "{\"error\":\"busy\"}"),
        ]);
        let mut client = Client::new(Address::Url(base), None, 1, None).unwrap();
        let error = get(&mut client, "info").unwrap_err();
        assert_eq!(error.0, "server error (503): busy");
        assert_eq!(server.join().unwrap(), 2);
    }

    #[test]
    fn no_retries_by_default() {
        let (base, server) = stub(vec![reply("429 Too Many Requests", "", "no")]);
        let mut client = Client::new(Address::Url(base), None, 0, None).unwrap();
        assert_eq!(get(&mut client, "info").unwrap_err().0, "server error (429): no");
        assert_eq!(server.join().unwrap(), 1);
    }

    #[test]
    fn rejected_token_is_noted_once() {
        let (base, server) = stub(vec![
            reply("200 OK", "x-token-valid: false\r\n", "{}"),
            reply("200 OK", "x-token-valid: false\r\n", "{}"),
        ]);
        let mut client = Client::new(Address::Url(base), None, 0, Some("secret".into())).unwrap();
        assert_eq!(client.token_valid, None);
        get(&mut client, "info").unwrap();
        assert_eq!(client.token_valid, Some(false));
        get(&mut client, "info").unwrap();
        assert_eq!(client.token_valid, Some(false));
        server.join().unwrap();
    }

    #[test]
    fn token_policy_matches_the_shared_table() {
        let table: Value = serde_json::from_str(include_str!("../../../tests/data/address_cases.json"))
            .expect("valid table");
        let cases = table["token_cases"].as_array().expect("token_cases");
        assert!(cases.len() >= 6);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let url = crate::address::normalize_base_url(case["url"].as_str().unwrap()).unwrap();
            let result = Client::new(Address::Url(url), None, 0, Some("secret".into()));
            if case["allowed"].as_bool().unwrap() {
                assert!(result.is_ok(), "case {name}");
            } else {
                let error = result.err().unwrap_or_else(|| panic!("case {name}: accepted"));
                assert!(error.0.contains("refusing to send a token"), "case {name}");
            }
        }
    }
}

//! Server address resolution, as documented in `docs/commands/address.md`.
//!
//! Precedence:
//!
//! 1. `--url`, then `--tcp`
//! 2. `--socket`
//! 3. `$SYLPHD_URL`
//! 4. `$SYLPHD_SOCKET`
//! 5. the default socket path (`$XDG_RUNTIME_DIR/sylphd.sock`, else
//!    `/tmp/sylphd-$UID.sock`)
//!
//! The address and token logic deliberately exists twice: here and in the Python
//! client (`client.connect`, which `sylphd.ncbitax` still needs). Both are tested
//! against the shared table `tests/data/address_cases.json`.

use std::fmt;
use std::path::PathBuf;

/// A resolved server address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Address {
    /// An `http://` or `https://` base URL, optionally with a path prefix.
    Url(String),
    /// A Unix socket path.
    Socket(PathBuf),
}

impl Address {
    /// Human description used in connection errors (`client._connection_message`).
    pub fn describe(&self) -> String {
        match self {
            Self::Url(url) => format!("at {url}"),
            Self::Socket(path) => format!("on {}", path.display()),
        }
    }
}

/// Address options of one client command (`--url`, `--tcp`, `--socket`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AddressArgs {
    pub url: Option<String>,
    pub tcp: Option<String>,
    pub socket: Option<String>,
}

/// Environment the resolution reads. A trait so tests can pin the UID and the
/// variables without touching the process environment.
pub trait Env {
    fn var(&self, name: &str) -> Option<String>;
    fn uid(&self) -> u32;
}

/// The real process environment.
#[derive(Debug, Clone, Copy, Default)]
pub struct ProcessEnv;

impl Env for ProcessEnv {
    fn var(&self, name: &str) -> Option<String> {
        std::env::var(name).ok()
    }

    fn uid(&self) -> u32 {
        uid_of_process()
    }
}

#[cfg(unix)]
fn uid_of_process() -> u32 {
    // `getuid`, declared here so the crate needs no libc dependency;
    // `uid_t` is `u32` on every supported Unix.
    unsafe extern "C" {
        fn getuid() -> u32;
    }
    // SAFETY: `getuid` has no preconditions and cannot fail.
    unsafe { getuid() }
}

#[cfg(not(unix))]
fn uid_of_process() -> u32 {
    0
}

/// Error of address/URL validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AddressError(pub String);

impl fmt::Display for AddressError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for AddressError {}

/// The default socket path.
pub fn default_socket_path(env: &dyn Env) -> PathBuf {
    match env.var("XDG_RUNTIME_DIR").filter(|v| !v.is_empty()) {
        Some(runtime) => PathBuf::from(runtime).join("sylphd.sock"),
        None => PathBuf::from("/tmp").join(format!("sylphd-{}.sock", env.uid())),
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.is_empty())
}

/// Resolves the server address with the documented precedence.
pub fn resolve_address(args: &AddressArgs, env: &dyn Env) -> Result<Address, AddressError> {
    // An explicit option wins even when its value is empty: an empty --url is
    // "empty server URL", not "unset".
    if let Some(url) = &args.url {
        return Ok(Address::Url(normalize_base_url(url)?));
    }
    if let Some(tcp) = &args.tcp {
        return Ok(Address::Url(normalize_base_url(tcp)?));
    }
    if let Some(socket) = &args.socket {
        return Ok(Address::Socket(PathBuf::from(socket)));
    }
    if let Some(url) = non_empty(env.var("SYLPHD_URL")) {
        return Ok(Address::Url(normalize_base_url(&url)?));
    }
    if let Some(socket) = non_empty(env.var("SYLPHD_SOCKET")) {
        return Ok(Address::Socket(PathBuf::from(socket)));
    }
    Ok(Address::Socket(default_socket_path(env)))
}

/// Validates and normalises a server base URL (`client.normalize_base_url`).
///
/// Accepts `http://` and `https://` (with an optional path prefix) and a bare
/// `host:port`; rejects a query or fragment and a missing host. The returned
/// URL has no trailing slash on its path.
pub fn normalize_base_url(input: &str) -> Result<String, AddressError> {
    let text = input.trim();
    if text.is_empty() {
        return Err(AddressError("empty server URL".into()));
    }
    if text.contains('?') || text.contains('#') {
        return Err(AddressError(
            "server URL must not contain a query or fragment".into(),
        ));
    }
    let with_scheme = if text.contains("://") {
        text.to_string()
    } else {
        format!("http://{text}")
    };
    let url = reqwest::Url::parse(&with_scheme)
        .map_err(|_| AddressError(format!("invalid server URL {input:?}")))?;
    let scheme = url.scheme().to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return Err(AddressError(format!(
            "unsupported URL scheme {scheme:?}; use http:// or https://"
        )));
    }
    let Some(host) = url.host_str() else {
        return Err(AddressError(format!("server URL has no host: {input:?}")));
    };
    let mut authority = host.to_string();
    if let Some(port) = url.port() {
        authority.push(':');
        authority.push_str(&port.to_string());
    }
    let path = url.path().trim_end_matches('/');
    Ok(format!("{scheme}://{authority}{path}"))
}

/// True for hosts where a bearer token may be sent over plain HTTP:
/// `localhost` and the IPv4/IPv6 loopback literals.
pub fn is_local_host(url: &reqwest::Url) -> bool {
    match url.host() {
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        Some(url::Host::Domain(name)) => name.eq_ignore_ascii_case("localhost"),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::collections::HashMap;

    const CASES: &str = include_str!("../../../tests/data/address_cases.json");

    struct MapEnv(HashMap<String, String>, u32);

    impl Env for MapEnv {
        fn var(&self, name: &str) -> Option<String> {
            self.0.get(name).cloned()
        }
        fn uid(&self) -> u32 {
            self.1
        }
    }

    #[test]
    fn shared_address_table_matches_rust_resolution() {
        let fixture: Value = serde_json::from_str(CASES).expect("valid address case table");
        let cases = fixture["cases"].as_array().expect("cases array");
        assert!(cases.len() >= 10);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let args = AddressArgs {
                url: case["args"]["url"].as_str().map(str::to_string),
                tcp: case["args"]["tcp"].as_str().map(str::to_string),
                socket: case["args"]["socket"].as_str().map(str::to_string),
            };
            let uid = case["uid"].as_u64().unwrap_or(1000) as u32;
            let vars: HashMap<String, String> = case["env"]
                .as_object()
                .map(|obj| {
                    obj.iter()
                        .filter_map(|(k, v)| v.as_str().map(|v| (k.clone(), v.to_string())))
                        .collect()
                })
                .unwrap_or_default();
            let result = resolve_address(&args, &MapEnv(vars, uid));
            let expected = &case["expect"];
            match expected["kind"].as_str().unwrap() {
                "url" => {
                    let got = result.unwrap_or_else(|e| panic!("case {name}: {e}"));
                    assert_eq!(
                        got,
                        Address::Url(expected["value"].as_str().unwrap().to_string()),
                        "case {name}"
                    );
                }
                "socket" => {
                    let got = result.unwrap_or_else(|e| panic!("case {name}: {e}"));
                    assert_eq!(
                        got,
                        Address::Socket(PathBuf::from(expected["value"].as_str().unwrap())),
                        "case {name}"
                    );
                }
                "error" => {
                    let err = result.expect_err(&format!("case {name}: expected an error"));
                    let needle = expected["contains"].as_str().unwrap();
                    assert!(
                        err.0.contains(needle),
                        "case {name}: {:?} does not contain {needle:?}",
                        err.0
                    );
                }
                other => panic!("case {name}: unknown expectation {other}"),
            }
        }
    }

    #[test]
    fn empty_environment_falls_back_to_uid_socket() {
        let env = MapEnv(HashMap::new(), 1234);
        assert_eq!(
            resolve_address(&AddressArgs::default(), &env).unwrap(),
            Address::Socket(PathBuf::from("/tmp/sylphd-1234.sock"))
        );
    }
}

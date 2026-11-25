//! Native client for sylphd: address/token resolution, HTTP transport, and the
//! `profile`, `query`, `assembly`, `info` and `render` commands.
//!
//! The binary is `sylphd-client`; all rendering comes from `sylphd-core`.

pub mod address;
pub mod cli;
pub mod commands;
pub mod transport;

pub use sylphd_core::render::PROG;

#[cfg(test)]
mod version_tests {
    /// The binary must report the Python package's version.
    #[test]
    fn version_matches_python_package() {
        let init = include_str!("../../../src/sylphd/__init__.py");
        let line = init
            .lines()
            .find(|line| line.trim_start().starts_with("__version__"))
            .expect("__version__ in src/sylphd/__init__.py");
        let python = line
            .split('"')
            .nth(1)
            .or_else(|| line.split('\'').nth(1))
            .expect("a quoted version");
        assert_eq!(python, env!("CARGO_PKG_VERSION"));
    }
}

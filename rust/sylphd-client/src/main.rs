//! `sylphd-client`: the native command line client.

use clap::Parser;

fn main() {
    let cli = match sylphd_client::cli::Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) => {
            if !error.use_stderr() {
                // --help and --version go to stdout with exit code 0.
                let _ = error.print();
                std::process::exit(error.exit_code());
            }
            // Usage errors carry the same `sylphd: error:` prefix as every other failure.
            let text = error.to_string();
            let text = text.replacen("error:", "sylphd: error:", 1);
            eprint!("{text}");
            if !text.ends_with('\n') {
                eprintln!();
            }
            std::process::exit(error.exit_code());
        }
    };
    std::process::exit(sylphd_client::commands::run(cli));
}

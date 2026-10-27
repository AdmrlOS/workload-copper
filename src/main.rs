use clap::{Parser, Subcommand};
use copper_rover::engine;
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    name = "copper-rover",
    version,
    about = "Hardware-free Copper warehouse autonomy and native deterministic replay"
)]
struct Cli {
    #[command(subcommand)]
    command: Action,
}
#[derive(Subcommand)]
enum Action {
    /// Run the real Copper graph, recording every CopperList and flushed JSONL at 20 Hz.
    Record {
        #[arg(long)]
        log_base: PathBuf,
        #[arg(long)]
        telemetry: PathBuf,
        #[arg(long,default_value_t=800,value_parser=clap::value_parser!(u64).range(1..=1_000_000))]
        steps: u64,
        #[arg(long)]
        realtime: bool,
    },
    /// Inject only recorded sensor CuMsgs and re-execute the same Copper planner/controller.
    Replay {
        #[arg(long)]
        log_base: PathBuf,
        #[arg(long)]
        telemetry: PathBuf,
        #[arg(long)]
        expected: PathBuf,
        #[arg(long)]
        report: PathBuf,
    },
}
fn run() -> engine::Result<bool> {
    match Cli::parse().command {
        Action::Record {
            log_base,
            telemetry,
            steps,
            realtime,
        } => {
            println!(
                "{}",
                engine::record(&log_base, &telemetry, steps, realtime)?
            );
            Ok(true)
        }
        Action::Replay {
            log_base,
            telemetry,
            expected,
            report,
        } => {
            let v = engine::replay(&log_base, &telemetry, &expected, &report)?;
            println!("{}", serde_json::to_string(&v)?);
            Ok(v.verified)
        }
    }
}
fn main() {
    match run() {
        Ok(true) => {}
        Ok(false) => std::process::exit(1),
        Err(e) => {
            eprintln!("copper-rover: {e}");
            std::process::exit(1);
        }
    }
}

use std::process::Command;

fn main() {
    let version = Command::new("rustc")
        .arg("--version")
        .output()
        .ok()
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .unwrap_or_else(|| "rustc unavailable".to_owned());
    println!("cargo:rustc-env=RUSTC_VERSION={}", version.trim());
    println!("cargo:rerun-if-changed=build.rs");
}

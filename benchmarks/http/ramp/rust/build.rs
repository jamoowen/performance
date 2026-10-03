use std::process::Command;

fn main() {
    let output = Command::new("rustc")
        .arg("--version")
        .output()
        .expect("rustc available");
    let version = String::from_utf8(output.stdout).expect("utf-8 rustc version");
    println!("cargo:rustc-env=RAMP_RUSTC_VERSION={}", version.trim());
}

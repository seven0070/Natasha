//! System telemetry and information commands for Natasha desktop.

use serde::{Deserialize, Serialize};
use sysinfo::System;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemTelemetry {
    pub os_name: String,
    pub os_version: String,
    pub architecture: String,
    pub hostname: String,
    pub cpu_count: usize,
    pub total_memory_mb: u64,
    pub used_memory_mb: u64,
    pub app_version: String,
}

#[tauri::command]
pub async fn get_system_info() -> Result<SystemTelemetry, String> {
    let mut sys = System::new_all();
    sys.refresh_all();

    let os_name = System::name().unwrap_or_else(|| "Unknown".to_string());
    let os_version = System::os_version().unwrap_or_else(|| "Unknown".to_string());
    let hostname = System::host_name().unwrap_or_else(|| "localhost".to_string());
    let architecture = std::env::consts::ARCH.to_string();
    let cpu_count = sys.cpus().len();
    let total_memory_mb = sys.total_memory() / (1024 * 1024);
    let used_memory_mb = sys.used_memory() / (1024 * 1024);
    let app_version = env!("CARGO_PKG_VERSION").to_string();

    Ok(SystemTelemetry {
        os_name,
        os_version,
        architecture,
        hostname,
        cpu_count,
        total_memory_mb,
        used_memory_mb,
        app_version,
    })
}

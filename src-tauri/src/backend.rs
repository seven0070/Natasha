//! Natasha Desktop: Native Python Backend Process Lifecycle Manager.
//!
//! Handles:
//! - Discovering Python / .venv runtime
//! - Detecting already running backend on port 8000 (avoiding duplicate processes)
//! - Spawning `python -m apps.server --host 127.0.0.1 --port 8000` if needed
//! - Non-blocking health-checking (/api/system/health)
//! - Clean process termination on shutdown (no zombie processes)
//! - Controlled crash detection and recovery (max 3 restarts)

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use log::{info, warn};
use serde::{Deserialize, Serialize};

const DEFAULT_PORT: u16 = 8000;
const MAX_RESTART_ATTEMPTS: u32 = 3;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendStatus {
    pub running: bool,
    pub managed: bool,
    pub port: u16,
    pub pid: Option<u32>,
    pub health: String,
}

pub struct BackendManager {
    child: Arc<Mutex<Option<Child>>>,
    is_managed: Arc<AtomicBool>,
    port: u16,
    restart_count: Arc<AtomicU32>,
    repo_root: PathBuf,
}

impl BackendManager {
    pub fn new(repo_root: PathBuf) -> Self {
        Self {
            child: Arc::new(Mutex::new(None)),
            is_managed: Arc::new(AtomicBool::new(false)),
            port: DEFAULT_PORT,
            restart_count: Arc::new(AtomicU32::new(0)),
            repo_root,
        }
    }

    /// Check if the Natasha backend is responding on the given port.
    pub fn check_health(&self) -> bool {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(1500))
            .build();

        if let Ok(client) = client {
            let url = format!("http://127.0.0.1:{}/api/system/health", self.port);
            if let Ok(resp) = client.get(&url).send() {
                return resp.status().is_success();
            }
        }
        false
    }

    /// Locate the Python binary, preferring repository's .venv.
    fn find_python(&self) -> Option<PathBuf> {
        let venv_candidates = [
            self.repo_root.join(".venv").join("Scripts").join("python.exe"), // Windows
            self.repo_root.join(".venv").join("bin").join("python"),       // Unix
            PathBuf::from(".venv").join("Scripts").join("python.exe"),
            PathBuf::from(".venv").join("bin").join("python"),
        ];

        for candidate in &venv_candidates {
            if candidate.is_file() {
                return Some(candidate.clone());
            }
        }

        // Fallback to system PATH python
        #[cfg(target_os = "windows")]
        let system_py = PathBuf::from("python.exe");
        #[cfg(not(target_os = "windows"))]
        let system_py = PathBuf::from("python3");

        Some(system_py)
    }

    /// Start or connect to the Natasha backend service.
    pub fn start(&self) -> Result<BackendStatus, String> {
        // 1. Check if backend is already running
        if self.check_health() {
            info!("Natasha backend is already active on port {}.", self.port);
            self.is_managed.store(false, Ordering::SeqCst);
            return Ok(self.status());
        }

        // 2. Discover Python binary
        let python = self.find_python().ok_or_else(|| {
            "Could not locate Python runtime (.venv or system python).".to_string()
        })?;

        info!("Starting Natasha backend with Python: {:?}", python);

        // 3. Spawn server process
        let mut cmd = Command::new(&python);
        cmd.args(["-m", "apps.server", "--host", "127.0.0.1", "--port", &self.port.to_string()])
            .current_dir(&self.repo_root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let child = cmd.spawn().map_err(|e| format!("Failed to spawn backend process: {}", e))?;
        let pid = child.id();
        info!("Spawned backend process with PID: {}", pid);

        {
            let mut lock = self.child.lock().map_err(|_| "Poisoned lock".to_string())?;
            *lock = Some(child);
        }
        self.is_managed.store(true, Ordering::SeqCst);

        // 4. Poll health check with timeout (up to 12 seconds)
        let start_time = Instant::now();
        let timeout = Duration::from_secs(12);

        while start_time.elapsed() < timeout {
            if self.check_health() {
                info!("Natasha backend successfully healthy on port {}.", self.port);
                return Ok(self.status());
            }
            thread::sleep(Duration::from_millis(300));
        }

        warn!("Backend spawned but health check did not respond within timeout.");
        Ok(self.status())
    }

    /// Cleanly stop the backend if it was started by this desktop manager.
    pub fn stop(&self) {
        if !self.is_managed.load(Ordering::SeqCst) {
            info!("Backend is externally managed; leaving running.");
            return;
        }

        if let Ok(mut lock) = self.child.lock() {
            if let Some(mut child) = lock.take() {
                info!("Shutting down Natasha backend process (PID: {})...", child.id());
                let _ = child.kill();
                let _ = child.wait();
                info!("Backend process terminated cleanly.");
            }
        }
        self.is_managed.store(false, Ordering::SeqCst);
    }

    /// Restart the backend with crash prevention check.
    pub fn restart(&self) -> Result<BackendStatus, String> {
        let attempts = self.restart_count.fetch_add(1, Ordering::SeqCst);
        if attempts >= MAX_RESTART_ATTEMPTS {
            return Err("Exceeded maximum restart attempts (3). Please check backend logs.".to_string());
        }

        info!("Restarting Natasha backend (attempt {})...", attempts + 1);
        self.stop();
        thread::sleep(Duration::from_millis(500));
        self.start()
    }

    /// Get current backend telemetry status.
    pub fn status(&self) -> BackendStatus {
        let is_running = self.check_health();
        let is_managed = self.is_managed.load(Ordering::SeqCst);

        let pid = if let Ok(lock) = self.child.lock() {
            lock.as_ref().map(|c| c.id())
        } else {
            None
        };

        let health = if is_running {
            "healthy".to_string()
        } else {
            "offline".to_string()
        };

        BackendStatus {
            running: is_running,
            managed: is_managed,
            port: self.port,
            pid,
            health,
        }
    }
}

impl Drop for BackendManager {
    fn drop(&mut self) {
        self.stop();
    }
}

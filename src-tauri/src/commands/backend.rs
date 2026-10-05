//! Backend management commands for Natasha desktop.

use std::sync::Arc;
use tauri::State;

use crate::backend::{BackendManager, BackendStatus};

#[tauri::command]
pub async fn get_backend_status(
    backend: State<'_, Arc<BackendManager>>,
) -> Result<BackendStatus, String> {
    Ok(backend.status())
}

#[tauri::command]
pub async fn restart_backend(
    backend: State<'_, Arc<BackendManager>>,
) -> Result<BackendStatus, String> {
    backend.restart()
}

#[tauri::command]
pub async fn check_backend_health(
    backend: State<'_, Arc<BackendManager>>,
) -> Result<bool, String> {
    Ok(backend.check_health())
}

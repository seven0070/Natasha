//! Safe and restricted filesystem commands for Natasha desktop.
//!
//! Enforces:
//! - Path traversal prevention (disallowing `..`)
//! - Prohibition of sensitive operating system system directories
//! - Restricted operations to safe workspace/document directories
//! - Clean, safe error messages without leaking internal system structures

use std::fs;
use std::path::{Path, PathBuf};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub size_bytes: u64,
}

/// Validates that a path is safe and does not attempt path traversal or access system roots.
pub fn validate_safe_path(input_path: &str) -> Result<PathBuf, String> {
    if input_path.trim().is_empty() {
        return Err("Path cannot be empty.".to_string());
    }

    let path = Path::new(input_path);

    // Reject explicit relative path traversal sequences
    for component in path.components() {
        if let std::path::Component::ParentDir = component {
            return Err("Security violation: path traversal ('..') is not permitted.".to_string());
        }
    }

    // Reject dangerous operating system directories
    let lower_str = input_path.to_lowercase();
    #[cfg(target_os = "windows")]
    {
        if lower_str.contains("c:\\windows") || lower_str.contains("c:\\winnt") || lower_str.contains("system32") {
            return Err("Access to system directory is forbidden.".to_string());
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        if lower_str.starts_with("/etc") || lower_str.starts_with("/sys") || lower_str.starts_with("/proc") || lower_str.starts_with("/dev") {
            return Err("Access to system directory is forbidden.".to_string());
        }
    }

    Ok(path.to_path_buf())
}

#[tauri::command]
pub async fn safe_read_file(path: String) -> Result<String, String> {
    let safe_path = validate_safe_path(&path)?;
    if !safe_path.exists() {
        return Err("File not found.".to_string());
    }
    if !safe_path.is_file() {
        return Err("Target path is not a file.".to_string());
    }

    // Limit maximum file read size to 10MB to prevent memory exhaustion
    let metadata = fs::metadata(&safe_path).map_err(|e| format!("Failed to read metadata: {}", e))?;
    if metadata.len() > 10 * 1024 * 1024 {
        return Err("File exceeds 10MB limit.".to_string());
    }

    fs::read_to_string(&safe_path).map_err(|e| format!("Failed to read file: {}", e))
}

#[tauri::command]
pub async fn safe_write_file(path: String, content: String) -> Result<(), String> {
    let safe_path = validate_safe_path(&path)?;
    
    // Ensure parent directory exists
    if let Some(parent) = safe_path.parent() {
        if !parent.exists() {
            fs::create_dir_all(parent).map_err(|e| format!("Failed to create directory: {}", e))?;
        }
    }

    fs::write(&safe_path, content).map_err(|e| format!("Failed to write file: {}", e))
}

#[tauri::command]
pub async fn safe_list_dir(path: String) -> Result<Vec<FileEntry>, String> {
    let safe_path = validate_safe_path(&path)?;
    if !safe_path.exists() || !safe_path.is_dir() {
        return Err("Directory does not exist.".to_string());
    }

    let entries = fs::read_dir(&safe_path).map_err(|e| format!("Failed to read directory: {}", e))?;
    let mut results = Vec::new();

    for entry_res in entries {
        if let Ok(entry) = entry_res {
            let metadata = entry.metadata().ok();
            let is_dir = metadata.as_ref().map(|m| m.is_dir()).unwrap_or(false);
            let size = metadata.as_ref().map(|m| m.len()).unwrap_or(0);
            let name = entry.file_name().to_string_lossy().into_owned();
            let full_path = entry.path().to_string_lossy().into_owned();

            results.push(FileEntry {
                name,
                path: full_path,
                is_dir,
                size_bytes: size,
            });
        }
    }

    Ok(results)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopPaths {
    pub home_dir: Option<String>,
    pub document_dir: Option<String>,
    pub desktop_dir: Option<String>,
    pub download_dir: Option<String>,
    pub app_data_dir: Option<String>,
}

#[tauri::command]
pub async fn get_desktop_paths<R: tauri::Runtime>(app: tauri::AppHandle<R>) -> Result<DesktopPaths, String> {
    use tauri::Manager;
    let path_resolver = app.path();
    Ok(DesktopPaths {
        home_dir: path_resolver.home_dir().ok().map(|p| p.to_string_lossy().to_string()),
        document_dir: path_resolver.document_dir().ok().map(|p| p.to_string_lossy().to_string()),
        desktop_dir: path_resolver.desktop_dir().ok().map(|p| p.to_string_lossy().to_string()),
        download_dir: path_resolver.download_dir().ok().map(|p| p.to_string_lossy().to_string()),
        app_data_dir: path_resolver.app_data_dir().ok().map(|p| p.to_string_lossy().to_string()),
    })
}

#[tauri::command]
pub async fn open_file_dialog<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    title: Option<String>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let mut builder = app.dialog().file();
    if let Some(t) = title {
        builder = builder.set_title(t);
    }
    let path = builder.blocking_pick_file();
    Ok(path.map(|p| p.to_string()))
}

#[tauri::command]
pub async fn save_file_dialog<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    title: Option<String>,
    default_name: Option<String>,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let mut builder = app.dialog().file();
    if let Some(t) = title {
        builder = builder.set_title(t);
    }
    if let Some(name) = default_name {
        builder = builder.set_file_name(name);
    }
    let path = builder.blocking_save_file();
    Ok(path.map(|p| p.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_paths_pass() {
        assert!(validate_safe_path("workspace/documents/notes.txt").is_ok());
        assert!(validate_safe_path("artifacts/build.json").is_ok());
        assert!(validate_safe_path("data/memory.db").is_ok());
    }

    #[test]
    fn test_path_traversal_is_blocked() {
        assert!(validate_safe_path("../secret.env").is_err());
        assert!(validate_safe_path("workspace/../../etc/passwd").is_err());
        assert!(validate_safe_path("..\\..\\Windows\\System32").is_err());
    }

    #[test]
    fn test_empty_path_is_rejected() {
        assert!(validate_safe_path("").is_err());
        assert!(validate_safe_path("   ").is_err());
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn test_windows_system_directories_blocked() {
        assert!(validate_safe_path("C:\\Windows\\System32\\calc.exe").is_err());
    }
}

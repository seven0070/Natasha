//! Natasha Desktop Application Library
//!
//! Integrates:
//! - Native Tauri windowing and lifecycle management
//! - Managed local Python backend process
//! - System tray with quick actions
//! - Desktop capabilities: notifications, dialogs, clipboard, autostart, deep links
//! - Secure and restricted filesystem operations

pub mod backend;
pub mod commands;
pub mod tray;

use std::path::PathBuf;
use std::sync::Arc;
use std::thread;

use log::info;
use tauri::Emitter;

use backend::BackendManager;
use commands::{backend::*, fs::*, system::*, window::*};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 1. Determine repository root path (one level up from src-tauri or current dir)
    let repo_root = if PathBuf::from("src-tauri").is_dir() {
        PathBuf::from(".")
    } else if PathBuf::from("../backend").is_dir() {
        PathBuf::from("..")
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    };

    let backend_manager = Arc::new(BackendManager::new(repo_root));
    let backend_for_setup = Arc::clone(&backend_manager);
    let backend_for_exit = Arc::clone(&backend_manager);

    tauri::Builder::default()
        .manage(backend_manager)
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--autostart"]),
        ))
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![
            get_backend_status,
            restart_backend,
            check_backend_health,
            window_minimize,
            window_maximize,
            window_toggle_maximize,
            window_close,
            window_set_fullscreen,
            get_system_info,
            safe_read_file,
            safe_write_file,
            safe_list_dir,
        ])
        .setup(move |app| {
            info!("Initializing Natasha Desktop Application...");

            // Setup System Tray
            if let Err(e) = tray::setup_tray(app.handle()) {
                log::warn!("Could not initialize system tray: {}", e);
            }

            // Launch or connect to Natasha Python backend asynchronously
            let app_handle = app.handle().clone();
            let backend = backend_for_setup;
            thread::spawn(move || {
                match backend.start() {
                    Ok(status) => {
                        info!("Natasha backend is ready: {:?}", status);
                        let _ = app_handle.emit("natasha:backend-ready", status);
                    }
                    Err(e) => {
                        log::error!("Backend startup warning: {}", e);
                        let _ = app_handle.emit("natasha:backend-error", e);
                    }
                }
            });

            // Setup deep link handler if available
            #[cfg(any(target_os = "windows", target_os = "macos", target_os = "linux"))]
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                let app_handle_for_link = app.handle().clone();
                app.deep_link().on_open_url(move |event| {
                    for url in event.urls() {
                        let url_str = url.to_string();
                        info!("Received deep link: {}", url_str);
                        let _ = app_handle_for_link.emit("natasha:deep-link", url_str);
                    }
                });
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Natasha desktop application")
        .run(move |_app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                info!("Application exiting: cleaning up Natasha backend process...");
                backend_for_exit.stop();
            }
        });
}

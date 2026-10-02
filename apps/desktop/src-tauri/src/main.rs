#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

mod runtime;
#[cfg(windows)]
mod single_instance;

#[derive(Default)]
struct DesktopStatus {
    shortcut_errors: Mutex<Vec<String>>,
}

#[tauri::command]
fn shortcut_status(
    window: tauri::WebviewWindow,
    status: tauri::State<'_, DesktopStatus>,
) -> Result<Vec<String>, String> {
    authorize(&window)?;
    status
        .shortcut_errors
        .lock()
        .map(|value| value.clone())
        .map_err(|_| "快捷键状态不可用".to_string())
}

use runtime::{RuntimeConnection, RuntimeManager, RuntimeStatus};

fn authorize(window: &tauri::WebviewWindow) -> Result<(), String> {
    if !matches!(window.label(), "pet" | "admin") {
        return Err("此窗口不能访问艾拉运行时".into());
    }
    Ok(())
}
#[tauri::command]
fn runtime_connection(
    window: tauri::WebviewWindow,
    manager: tauri::State<'_, RuntimeManager>,
) -> Result<RuntimeConnection, String> {
    authorize(&window)?;
    manager.connection()
}
#[tauri::command]
fn runtime_status(
    window: tauri::WebviewWindow,
    manager: tauri::State<'_, RuntimeManager>,
) -> Result<RuntimeStatus, String> {
    authorize(&window)?;
    Ok(manager.status())
}
#[tauri::command]
async fn runtime_ensure(
    window: tauri::WebviewWindow,
    manager: tauri::State<'_, RuntimeManager>,
    restart: bool,
) -> Result<RuntimeStatus, String> {
    authorize(&window)?;
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || manager.ensure(restart))
        .await
        .map_err(|_| "运行时管理任务失败".to_string())?
}

#[tauri::command]
fn show_admin(app: tauri::AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("admin")
        .ok_or_else(|| "管理后台窗口不存在".to_string())?;
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

fn main() {
    #[cfg(windows)]
    let _instance = match single_instance::acquire() {
        Some(instance) => instance,
        None => return,
    };
    let application = tauri::Builder::default()
        .manage(DesktopStatus::default())
        .on_window_event(|window, event| {
            if window.label() == "admin" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .setup(|app| {
            let manager =
                RuntimeManager::new(app.path().resource_dir()?).map_err(std::io::Error::other)?;
            manager.monitor();
            app.manage(manager);
            let toggle = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyE);
            let talk = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyV);
            let registered = toggle.clone();
            let registered_talk = talk.clone();
            app.handle().plugin(
                tauri_plugin_global_shortcut::Builder::new()
                    .with_handler(move |app, shortcut, event| {
                        if shortcut == &registered && event.state() == ShortcutState::Pressed {
                            if let Some(window) = app.get_webview_window("pet") {
                                let _ = window.show();
                                let _ = window.set_focus();
                                let _ = show_admin(app.clone());
                            }
                        } else if shortcut == &registered_talk
                            && event.state() == ShortcutState::Pressed
                        {
                            if let Some(window) = app.get_webview_window("pet") {
                                let _ = window.show();
                                let _ = window.set_focus();
                                let _ = window.emit("ella-toggle-voice", ());
                            }
                        }
                    })
                    .build(),
            )?;
            for (shortcut, label) in [(toggle, "Ctrl+Alt+E"), (talk, "Ctrl+Alt+V")] {
                if app.global_shortcut().register(shortcut).is_err() {
                    if let Ok(mut errors) = app.state::<DesktopStatus>().shortcut_errors.lock() {
                        errors.push(format!(
                            "{} 未能注册，可能被其他程序占用；可从人物菜单操作。",
                            label
                        ));
                    }
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            show_admin,
            runtime_connection,
            runtime_status,
            runtime_ensure,
            shortcut_status
        ])
        .build(tauri::generate_context!())
        .expect("启动艾拉桌面端失败");
    application.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            app.state::<RuntimeManager>().close();
        }
    });
}

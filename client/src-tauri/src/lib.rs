// lib.rs
use std::collections::HashMap;
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::ShellExt;
use tokio::io::{self, AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::windows::named_pipe::ClientOptions;

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

// 新增：从 main.rs 剪切过来
#[tauri::command]
fn open_external_player(video_path: String, player_path: String) {
    println!("准备播放视频: {} \n使用播放器: {}", video_path, player_path);

    match Command::new(&player_path).arg(&video_path).spawn() {
        Ok(_) => println!("播放器启动成功"),
        Err(e) => eprintln!("拉起播放器失败: {}", e),
    }
}

/// 用内置mpv播放:把从当前集开始的剩余集数整季喂给mpv做播放列表,
/// 它自己负责连播;我们这边单独开一个具名管道监听mpv的start-file事件,
/// 每切到一集(不管是不是自动连播切过去的)就通知前端乐观标记已看,
/// 跟外置播放器"点击即标记"是同一套逻辑,只是把它延伸到自动连播的后续集数上。
/// --save-position-on-quit让mpv自己记住播放进度,退出/重新打开同一个文件时自动续播,
/// 跟PotPlayer的记忆播放位置体验一致。
#[tauri::command]
fn open_builtin_player(app: AppHandle, video_paths: Vec<String>) -> Result<String, String> {
    if video_paths.is_empty() {
        return Err("没有可播放的文件".into());
    }

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let pipe_name = format!(r"\\.\pipe\hamstash-mpv-{}", ts);

    let mut args = vec![
        format!("--input-ipc-server={}", pipe_name),
        "--save-position-on-quit=yes".to_string(),
    ];
    args.extend(video_paths.clone());

    let sidecar = app
        .shell()
        .sidecar("mpv")
        .map_err(|e| format!("找不到mpv sidecar: {}", e))?
        .args(args);

    let (mut rx, _child) = sidecar.spawn().map_err(|e| format!("拉起mpv失败: {}", e))?;

    // 消费掉mpv自身stdout/stderr事件流,避免channel堆积;进度不靠这个,靠IPC管道。
    tauri::async_runtime::spawn(async move { while let Some(_event) = rx.recv().await {} });

    let app_for_ipc = app.clone();
    let pipe_for_ipc = pipe_name.clone();
    tauri::async_runtime::spawn(async move {
        run_mpv_ipc_listener(app_for_ipc, pipe_for_ipc, video_paths).await;
    });

    Ok(pipe_name)
}

async fn run_mpv_ipc_listener(app: AppHandle, pipe_name: String, video_paths: Vec<String>) {
    // mpv创建具名管道需要一点时间,重试连接(最多约10秒)。
    let mut client = None;
    for _ in 0..50 {
        match ClientOptions::new().open(&pipe_name) {
            Ok(c) => {
                client = Some(c);
                break;
            }
            Err(_) => tokio::time::sleep(Duration::from_millis(200)).await,
        }
    }
    let client = match client {
        Some(c) => c,
        None => {
            eprintln!("[mpv-ipc] 连接命名管道超时: {}", pipe_name);
            return;
        }
    };

    let (read_half, mut write_half) = io::split(client);
    let mut lines = BufReader::new(read_half).lines();

    // 先拿playlist的权威 id -> filename 对照,不假设mpv分配的playlist_entry_id顺序。
    let query = serde_json::json!({ "command": ["get_property", "playlist"] });
    if let Ok(mut payload) = serde_json::to_vec(&query) {
        payload.push(b'\n');
        let _ = write_half.write_all(&payload).await;
    }

    let mut id_to_path: HashMap<i64, String> = HashMap::new();

    loop {
        let line = match lines.next_line().await {
            Ok(Some(l)) => l,
            _ => break, // 管道关闭/mpv退出,监听任务结束
        };

        let parsed: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };

        if let Some(playlist) = parsed.get("data").and_then(|d| d.as_array()) {
            for (idx, entry) in playlist.iter().enumerate() {
                let id = entry.get("id").and_then(|v| v.as_i64());
                let filename = entry.get("filename").and_then(|v| v.as_str());
                match (id, filename) {
                    (Some(id), Some(filename)) => {
                        id_to_path.insert(id, filename.to_string());
                    }
                    _ => {
                        // 兜底:响应里缺字段时,按顺序对应我们自己传入的路径列表
                        if let Some(p) = video_paths.get(idx) {
                            id_to_path.insert((idx + 1) as i64, p.clone());
                        }
                    }
                }
            }
            continue;
        }

        // 用start-file而不是end-file:目的是"切到这一集就乐观标记",不等播完,
        // 跟外置播放器点击即标记的语义保持一致。第一集的start-file大概率在我们
        // 连上管道之前就已经发生、会错过——没关系,那一集在前端点击的瞬间就已经标记过了,
        // 这里只负责补上自动连播切到的后续集数。
        if parsed.get("event").and_then(|v| v.as_str()) == Some("start-file") {
            let path = parsed
                .get("playlist_entry_id")
                .and_then(|v| v.as_i64())
                .and_then(|id| id_to_path.get(&id).cloned())
                .unwrap_or_default();

            if !path.is_empty() {
                let _ = app.emit("mpv-episode-started", serde_json::json!({ "path": path }));
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(tauri::generate_handler![
            open_external_player,
            open_builtin_player,
            greet
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

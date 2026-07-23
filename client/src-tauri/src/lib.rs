// lib.rs
use std::collections::HashMap;
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Webview, WebviewBuilder, WebviewUrl};
use tauri_plugin_shell::ShellExt;
use tokio::io::{self, AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::windows::named_pipe::ClientOptions;

const BGM_EMBED_LABEL: &str = "bgm-embed";
const MAIN_WINDOW_LABEL: &str = "main";

/// 当前展示中的"内嵌Bangumi详情页"子webview,连同创建它时用的代理地址一起记着——
/// 子webview的代理是创建时固化进WebView2 environment的,没法热改,
/// 只能靠比对这个值来决定要不要销毁重建。
struct BgmEmbed {
    webview: Webview,
    proxy: Option<String>,
}

/// 同一时间只需要一个内嵌webview,不用HashMap管理多个。
/// 用tokio的异步Mutex(而不是std::sync::Mutex)是因为下面要跨await持有它:
/// "查状态-读代理设置-创建"必须是一个不可被打断的整体,详见embed_bgm_webview。
struct BgmEmbedState(tokio::sync::Mutex<Option<BgmEmbed>>);

/// 在详情页里"内嵌"打开Bangumi详情页,用的是Tauri的"子webview"机制
/// (window.add_child,而不是新开一个独立窗口)——子webview可以单独设代理(.proxy_url),
/// 只影响这一个子webview,不会连累主窗口(承载React应用、访问本地后端127.0.0.1:8080)。
/// 子webview在父窗口里的位置/大小由前端传入(对应详情页里那块占位div在屏幕上的实际位置),
/// 视觉上就是"内嵌"在页面布局里的效果。
///
/// 如果子webview已经存在(比如从一部番切到另一部),直接复用、导航到新地址+更新位置大小,
/// 不重新创建(创建时才会读一次代理设置,复用能避免每次切换番剧都重新读设置/重建的开销);
/// 只有代理地址变了才销毁重建,因为代理没法在已有webview上热改。
#[tauri::command]
async fn embed_bgm_webview(
    app: AppHandle,
    state: tauri::State<'_, BgmEmbedState>,
    bgm_id: i64,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let target = format!("https://bgm.tv/subject/{}", bgm_id);
    let target_url = target.parse().map_err(|e| format!("URL解析失败: {}", e))?;
    let position = LogicalPosition::new(x, y);
    let size = LogicalSize::new(width, height);

    // 整个"查状态-读代理设置-创建"流程都在同一把异步锁里,中途不放手。
    // 前端是React.StrictMode,dev下effect会mount->cleanup->mount,进详情页时
    // 这个命令会被连着触发两次;之前的写法在await读代理设置前就把锁松开了,
    // 第二次进来时第一次的add_child还没落地、状态里还是None,于是两次都去创建
    // 同一个label,后一次必然报"a webview with label bgm-embed already exists"。
    let mut guard = state.0.lock().await;

    // 拿不到/请求失败就当作没配代理,不阻断功能,降级成直连行为。
    let proxy_url = fetch_proxy_url().await;

    match guard.as_ref().map(|embed| embed.proxy == proxy_url) {
        Some(true) => {
            let embed = guard.as_ref().ok_or_else(|| "内嵌webview状态异常".to_string())?;
            embed
                .webview
                .navigate(target_url)
                .map_err(|e| format!("导航失败: {}", e))?;
            embed
                .webview
                .set_position(position)
                .map_err(|e| format!("设置位置失败: {}", e))?;
            embed
                .webview
                .set_size(size)
                .map_err(|e| format!("设置大小失败: {}", e))?;
            return Ok(());
        }
        // 代理设置变了:销毁重建,否则改完设置还在用老代理(表现为"改了还是白的")。
        Some(false) => {
            if let Some(old) = guard.take() {
                let _ = old.webview.close();
            }
        }
        None => {}
    }

    let window = app
        .get_window(MAIN_WINDOW_LABEL)
        .ok_or_else(|| "找不到主窗口".to_string())?;

    // 页面加载完成时通知前端,让它把"加载中"的占位状态收掉。加载不出来时
    // (国内网络连不上bgm.tv)这个事件永远不来,前端靠超时给出可操作的提示,
    // 而不是把用户晾在一块没有任何反馈的白框前面。
    let mut builder = WebviewBuilder::new(BGM_EMBED_LABEL, WebviewUrl::External(target_url))
        .on_page_load(|webview, payload| {
            if payload.event() == PageLoadEvent::Finished {
                let _ = webview.app_handle().emit("bgm-embed-loaded", ());
            }
        });

    if let Some(proxy) = &proxy_url {
        match proxy.parse() {
            Ok(parsed) => {
                builder = builder.proxy_url(parsed);
                // wry是把代理拼成--proxy-server=塞进WebView2 environment的
                // AdditionalBrowserArguments里的,而environment按data_directory复用。
                // 不单独指定目录的话这个子webview会跟主窗口共用同一个用户数据目录,
                // 但WebView2要求同一个用户数据目录下的browser arguments必须一致——
                // 结果就是上面这行proxy_url要么让创建直接失败,要么被静默忽略、
                // 沿用主窗口那份(没有代理)。给它一个自己的目录才能拿到独立的
                // environment,proxy_url才真正生效。
                match app.path().app_local_data_dir() {
                    Ok(dir) => builder = builder.data_directory(dir.join("bgm-embed")),
                    Err(e) => eprintln!(
                        "[embed_bgm_webview] 取应用数据目录失败,代理可能不生效: {}",
                        e
                    ),
                }
            }
            Err(_) => eprintln!("[embed_bgm_webview] 代理地址解析失败,忽略: {}", proxy),
        }
    }

    let webview = window
        .add_child(builder, position, size)
        .map_err(|e| format!("创建内嵌webview失败: {}", e))?;

    *guard = Some(BgmEmbed {
        webview,
        proxy: proxy_url,
    });
    Ok(())
}

/// 详情页所在区域尺寸/位置变化时(比如窗口resize)调用,同步更新已存在的子webview。
/// 还没创建子webview时(比如用户还没打开过详情页)直接忽略,不算错误。
#[tauri::command]
async fn resize_bgm_webview(
    state: tauri::State<'_, BgmEmbedState>,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let guard = state.0.lock().await;
    if let Some(embed) = guard.as_ref() {
        embed
            .webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|e| format!("设置位置失败: {}", e))?;
        embed
            .webview
            .set_size(LogicalSize::new(width, height))
            .map_err(|e| format!("设置大小失败: {}", e))?;
    }
    Ok(())
}

/// 离开详情页时调用,销毁子webview——不清理的话它会作为独立于React渲染树之外的
/// 原生图层,继续悬停在原来的屏幕位置上,盖住切换过去的其他页面内容。
#[tauri::command]
async fn close_bgm_webview(state: tauri::State<'_, BgmEmbedState>) -> Result<(), String> {
    let mut guard = state.0.lock().await;
    if let Some(embed) = guard.take() {
        embed.webview.close().map_err(|e| format!("关闭失败: {}", e))?;
    }
    Ok(())
}

/// 读后端的effective_proxy_url:设置页手填的地址优先,留空时是后端探测到的系统代理
/// (见server/services/common.py的_detect_system_proxy)。读proxy_url的话,
/// 用户明明开着系统代理却没在设置页填过,这里就会拿到空值、白白降级成直连。
async fn fetch_proxy_url() -> Option<String> {
    let resp = reqwest::get("http://127.0.0.1:8080/settings")
        .await
        .ok()?;
    let data: serde_json::Value = resp.json().await.ok()?;
    let proxy = data.get("effective_proxy_url")?.as_str()?.trim().to_string();
    if proxy.is_empty() {
        None
    } else {
        Some(proxy)
    }
}

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
        .manage(BgmEmbedState(tokio::sync::Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            open_external_player,
            open_builtin_player,
            embed_bgm_webview,
            resize_bgm_webview,
            close_bgm_webview,
            greet
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

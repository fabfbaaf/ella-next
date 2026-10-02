use serde::Serialize;
use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

#[derive(Clone, Serialize)]
pub struct RuntimeStatus {
    pub state: String,
    pub owned: bool,
    pub error: String,
    pub log_path: String,
    pub base_url: String,
}
#[derive(Serialize)]
pub struct RuntimeConnection {
    pub base_url: String,
    pub token: String,
}
struct Inner {
    child: Option<Child>,
    status: RuntimeStatus,
}
#[derive(Clone)]
pub struct RuntimeManager {
    inner: Arc<Mutex<Inner>>,
    busy: Arc<AtomicBool>,
    closed: Arc<AtomicBool>,
    data_dir: PathBuf,
    resource_dir: PathBuf,
    port: u16,
}
fn runtime_command(resource_dir: &Path, source: &Path, debug: bool) -> Result<Command, String> {
    if debug && source.join("run_runtime.py").is_file() {
        let python = source.join(".venv/Scripts/python.exe");
        if !python.is_file() {
            return Err("项目缺少 .venv Python 环境".into());
        }
        let mut command = Command::new(python);
        command
            .arg("-B")
            .arg(source.join("run_runtime.py"))
            .current_dir(source)
            .env("ELLA_RESOURCES_DIR", source);
        return Ok(command);
    }
    let packaged = resource_dir.join("runtime/ella-runtime/ella-runtime.exe");
    if !packaged.is_file() {
        return Err(
            "安装包缺少本地运行时，请使用 scripts/package-desktop.ps1 构建完整安装包".into(),
        );
    }
    let mut command = Command::new(packaged);
    command.env("ELLA_RESOURCES_DIR", resource_dir);
    Ok(command)
}

impl RuntimeManager {
    pub fn new(resource_dir: PathBuf) -> Result<Self, String> {
        let data_dir = std::env::var_os("ELLA_DATA_DIR")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("LOCALAPPDATA").map(|p| PathBuf::from(p).join("EllaNext")))
            .ok_or("找不到艾拉本地数据目录")?;
        fs::create_dir_all(&data_dir).map_err(|_| "无法创建艾拉数据目录")?;
        let port = std::env::var("ELLA_RUNTIME_PORT")
            .unwrap_or_else(|_| "8766".into())
            .parse::<u16>()
            .map_err(|_| "运行时端口配置无效")?;
        if port == 0 {
            return Err("运行时端口不能为零".into());
        }
        let data_dir = if data_dir.is_absolute() {
            data_dir
        } else {
            std::env::current_dir()
                .map_err(|_| "无法确定数据目录")?
                .join(data_dir)
        };
        let status = RuntimeStatus {
            state: "starting".into(),
            owned: false,
            error: String::new(),
            log_path: data_dir.join("runtime.log").to_string_lossy().into(),
            base_url: format!("http://127.0.0.1:{port}"),
        };
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                child: None,
                status,
            })),
            busy: Arc::new(AtomicBool::new(false)),
            closed: Arc::new(AtomicBool::new(false)),
            data_dir,
            resource_dir,
            port,
        })
    }
    pub fn status(&self) -> RuntimeStatus {
        self.inner.lock().unwrap().status.clone()
    }
    fn token(&self) -> Result<String, String> {
        let value = fs::read_to_string(self.data_dir.join("runtime.session"))
            .map_err(|_| "运行时尚未就绪，请稍候或在后台重试")?;
        if value.len() < 32
            || value.len() > 512
            || !value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            return Err("运行时会话凭据无效".into());
        }
        Ok(value)
    }
    pub fn connection(&self) -> Result<RuntimeConnection, String> {
        let status = self.status();
        if status.state != "running" {
            return Err(if status.error.is_empty() {
                "运行时正在启动，请稍候".into()
            } else {
                status.error
            });
        }
        Ok(RuntimeConnection {
            base_url: self.status().base_url,
            token: self.token()?,
        })
    }
    fn probe(&self) -> bool {
        let Ok(token) = self.token() else {
            return false;
        };
        let addr: SocketAddr = format!("127.0.0.1:{}", self.port).parse().unwrap();
        let Ok(mut socket) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
            return false;
        };
        let _ = socket.set_read_timeout(Some(Duration::from_millis(1500)));
        let _ = socket.set_write_timeout(Some(Duration::from_millis(500)));
        let request = format!("GET /api/runtime/identity HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\nConnection: close\r\n\r\n", self.port, token);
        if socket.write_all(request.as_bytes()).is_err() {
            return false;
        }
        let mut bytes = Vec::new();
        if socket.take(16384).read_to_end(&mut bytes).is_err() {
            return false;
        }
        let response = String::from_utf8_lossy(&bytes);
        let Some((headers, body)) = response.split_once("\r\n\r\n") else {
            return false;
        };
        if !(headers.starts_with("HTTP/1.1 200 ") || headers.starts_with("HTTP/1.0 200 ")) {
            return false;
        }
        let Ok(identity) = serde_json::from_str::<serde_json::Value>(body) else {
            return false;
        };
        identity["application"] == "ella-next"
            && identity["data_dir"]
                .as_str()
                .is_some_and(|p| fs::canonicalize(p).ok() == fs::canonicalize(&self.data_dir).ok())
    }
    fn spawn(&self) -> Result<Child, String> {
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let mut command = runtime_command(&self.resource_dir, &source, cfg!(debug_assertions))?;
        let log = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data_dir.join("runtime.log"))
            .map_err(|_| "无法写入运行时日志")?;
        command
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|_| "无法打开运行时日志")?)
            .stderr(log)
            .env("ELLA_DATA_DIR", &self.data_dir)
            .env("ELLA_RUNTIME_HOST", "127.0.0.1")
            .env("ELLA_RUNTIME_PORT", self.port.to_string());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        command
            .spawn()
            .map_err(|_| "运行时进程启动失败，请查看本地日志".into())
    }
    pub fn ensure(&self, restart: bool) -> Result<RuntimeStatus, String> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Ok(self.status());
        }
        let result = self.ensure_inner(restart);
        self.busy.store(false, Ordering::SeqCst);
        if let Err(ref error) = result {
            let mut inner = self.inner.lock().unwrap();
            inner.status.state = "error".into();
            inner.status.error = error.clone();
        }
        result
    }
    fn ensure_inner(&self, restart: bool) -> Result<RuntimeStatus, String> {
        if self.closed.load(Ordering::SeqCst) {
            return Err("桌面应用正在退出".into());
        }
        if restart {
            let mut inner = self.inner.lock().unwrap();
            if let Some(mut child) = inner.child.take() {
                self.stop_child(&mut child);
            } else if inner.status.state == "running" {
                return Err("当前运行时由外部启动，请在原窗口重启；艾拉不会关闭外部进程".into());
            }
            inner.status.owned = false;
        }
        if self.probe() {
            let mut inner = self.inner.lock().unwrap();
            inner.status.state = "running".into();
            inner.status.error.clear();
            return Ok(inner.status.clone());
        }
        {
            let mut inner = self.inner.lock().unwrap();
            if let Some(child) = inner.child.as_mut() {
                if child
                    .try_wait()
                    .map_err(|_| "无法读取运行时进程状态")?
                    .is_some()
                {
                    inner.child = None;
                }
            }
            if inner.child.is_none() {
                let addr: SocketAddr = format!("127.0.0.1:{}", self.port).parse().unwrap();
                if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() {
                    return Err(
                        "运行时端口已占用，且不是可验证的艾拉服务；请检查原运行时或端口设置".into(),
                    );
                }
                inner.child = Some(self.spawn()?);
                inner.status.owned = true;
            }
            inner.status.state = "starting".into();
            inner.status.error.clear();
        }
        for _ in 0..50 {
            if self.closed.load(Ordering::SeqCst) {
                return Err("桌面应用正在退出".into());
            }
            if self.probe() {
                let mut inner = self.inner.lock().unwrap();
                inner.status.state = "running".into();
                return Ok(inner.status.clone());
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        Err("运行时没有就绪，请查看 runtime.log 后重试".into())
    }
    fn stop_child(&self, child: &mut Child) {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        if let Ok(token) = self.token() {
            let address: SocketAddr = format!("127.0.0.1:{}", self.port).parse().unwrap();
            if let Ok(mut socket) = TcpStream::connect_timeout(&address, Duration::from_millis(500))
            {
                let _ = socket.set_write_timeout(Some(Duration::from_millis(500)));
                let request = format!("POST /api/runtime/shutdown HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", self.port, token);
                if socket.write_all(request.as_bytes()).is_ok() {
                    for _ in 0..80 {
                        if child.try_wait().ok().flatten().is_some() {
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(100));
                    }
                }
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    pub fn monitor(&self) {
        let manager = self.clone();
        std::thread::spawn(move || {
            while !manager.closed.load(Ordering::SeqCst) {
                let _ = manager.ensure(false);
                for _ in 0..30 {
                    if manager.closed.load(Ordering::SeqCst) {
                        return;
                    }
                    std::thread::sleep(Duration::from_secs(1));
                }
            }
        });
    }
    pub fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        let mut inner = self.inner.lock().unwrap();
        if let Some(mut child) = inner.child.take() {
            self.stop_child(&mut child);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    fn fixture(listener: &TcpListener) -> RuntimeManager {
        let port = listener.local_addr().unwrap().port();
        let directory =
            std::env::temp_dir().join(format!("ella-native-test-{}-{port}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("runtime.session"), "a".repeat(64)).unwrap();
        let status = RuntimeStatus {
            state: "starting".into(),
            owned: false,
            error: String::new(),
            log_path: String::new(),
            base_url: format!("http://127.0.0.1:{port}"),
        };
        RuntimeManager {
            inner: Arc::new(Mutex::new(Inner {
                child: None,
                status,
            })),
            busy: Arc::new(AtomicBool::new(false)),
            closed: Arc::new(AtomicBool::new(false)),
            data_dir: directory,
            resource_dir: PathBuf::new(),
            port,
        }
    }
    fn command_fixture(name: &str) -> (PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "ella-runtime-command-{}-{name}",
            std::process::id()
        ));
        let source = root.join("source");
        let resources = root.join("resources");
        fs::create_dir_all(source.join(".venv/Scripts")).unwrap();
        fs::create_dir_all(resources.join("runtime/ella-runtime")).unwrap();
        fs::write(source.join("run_runtime.py"), "source entry").unwrap();
        fs::write(source.join(".venv/Scripts/python.exe"), "fake python").unwrap();
        fs::write(
            resources.join("runtime/ella-runtime/ella-runtime.exe"),
            "stale packaged entry",
        )
        .unwrap();
        (source, resources)
    }
    #[test]
    fn debug_uses_current_source_even_when_packaged_runtime_exists() {
        let (source, resources) = command_fixture("debug-source");
        let command = runtime_command(&resources, &source, true).unwrap();
        assert_eq!(
            Path::new(command.get_program()),
            source.join(".venv/Scripts/python.exe")
        );
        assert_eq!(command.get_current_dir(), Some(source.as_path()));
        let args: Vec<_> = command.get_args().collect();
        assert_eq!(
            args,
            vec![
                std::ffi::OsStr::new("-B"),
                source.join("run_runtime.py").as_os_str()
            ]
        );
        assert!(command
            .get_envs()
            .any(|(key, value)| key == "ELLA_RESOURCES_DIR" && value == Some(source.as_os_str())));
    }
    #[test]
    fn source_without_python_reports_missing_environment_instead_of_using_stale_package() {
        let (source, resources) = command_fixture("missing-python");
        fs::remove_file(source.join(".venv/Scripts/python.exe")).unwrap();
        assert!(runtime_command(&resources, &source, true)
            .unwrap_err()
            .contains(".venv"));
    }
    #[test]
    fn release_uses_packaged_runtime_and_packaged_resources() {
        let (source, resources) = command_fixture("release-package");
        let command = runtime_command(&resources, &source, false).unwrap();
        assert_eq!(
            Path::new(command.get_program()),
            resources.join("runtime/ella-runtime/ella-runtime.exe")
        );
        assert!(command.get_envs().any(
            |(key, value)| key == "ELLA_RESOURCES_DIR" && value == Some(resources.as_os_str())
        ));
    }
    #[test]
    fn release_without_package_never_runs_development_source() {
        let (source, resources) = command_fixture("release-missing");
        fs::remove_file(resources.join("runtime/ella-runtime/ella-runtime.exe")).unwrap();
        assert!(runtime_command(&resources, &source, false).is_err());
    }
    #[test]
    fn identity_probe_reuses_only_authenticated_ella_runtime() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let manager = fixture(&listener);
        let directory = manager.data_dir.to_string_lossy().to_string();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];
            let length = socket.read(&mut request).unwrap();
            let text = String::from_utf8_lossy(&request[..length]);
            assert!(text.starts_with("GET /api/runtime/identity"));
            assert!(text.contains("Authorization: Bearer "));
            let body =
                serde_json::json!({"application":"ella-next","data_dir":directory}).to_string();
            socket
                .write_all(
                    format!(
                        "HTTP/1.0 200 OK\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    )
                    .as_bytes(),
                )
                .unwrap();
        });
        let status = manager.ensure(false).unwrap();
        assert_eq!(status.state, "running");
        assert!(!status.owned);
        assert!(manager.inner.lock().unwrap().child.is_none());
        server.join().unwrap();
        manager.close();
    }
    #[test]
    fn unrelated_service_is_not_reused() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let manager = fixture(&listener);
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];
            let _ = socket.read(&mut request);
            socket
                .write_all(b"HTTP/1.0 200 OK\r\n\r\n{\"application\":\"another-app\"}")
                .unwrap();
        });
        assert!(!manager.probe());
        server.join().unwrap();
    }
}

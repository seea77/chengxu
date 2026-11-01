# -*- coding: utf-8 -*-
"""
计分大屏 —— 桌面版启动器（通用计分大屏，不限于摄影班）
将原本的单个 HTML 文件包装成可双击运行的原生窗口程序。

依赖：Windows 上通过 pywebview 调用系统自带的 Microsoft Edge WebView2 运行时渲染页面。
      启动前会检测该运行时是否可用；缺失时弹出提示并引导到官方下载页。

便携数据方案（满足"拷贝到别的电脑就能看到数据，无需导入导出"）：
- 页面数据统一保存在 exe 同目录下的 `date.json`（侧车文件）。
- 本地服务器在返回页面时，把该侧车文件注入到 <script id="embeddedState"> 中，
  页面 load() 在 localStorage 为空时会读取它 —— 因此把 exe + 这个 json 一起拷贝到
  另一台电脑，打开即可看到原有数据。
- 页面每次保存(save)会向 /_save 发送数据，服务器写回侧车文件。
"""

import os
import sys
import re
import json
import ctypes
import webbrowser
import winreg
import http.server
import socketserver
import threading

PORT = 8731
SIDECAR_NAME = "date.json"
OLD_SIDECAR_NAME = "摄影班计分数据.json"  # 旧版数据文件名，首次运行自动迁移


def get_base():
    """开发时返回脚本所在目录；打包后返回 PyInstaller 解压目录。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_data_dir():
    """侧车数据与 WebView 配置文件统一放在 exe 同级目录（保证可随 exe 拷贝）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return get_base()


BASE = get_base()
DATA_DIR = get_data_dir()
SIDECAR = os.path.join(DATA_DIR, SIDECAR_NAME)
OLD_SIDECAR = os.path.join(DATA_DIR, OLD_SIDECAR_NAME)

# 按 exe 名自动选择要加载的页面：含“摄影”用摄影版，否则用通用版
_EXEC_NAME = os.path.splitext(
    os.path.basename(sys.executable if getattr(sys, "frozen", False) else __file__)
)[0]
PAGE_FILE = "index_photo.html" if "摄影" in _EXEC_NAME else "index.html"


# ---------------------------------------------------------------------------
# WebView2 运行时检测：pywebview 在 Windows 上依赖系统自带的 Edge WebView2 运行时。
# 若该组件缺失，pywebview 会"静默失败"（窗口打开但页面黑屏、且不抛异常），
# 因此必须在启动前主动检测，缺失时给出提示并引导到官方下载页。
# ---------------------------------------------------------------------------
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/"
# 常青版 x64 引导安装包直链（备用，写进提示框文本）
WEBVIEW2_DIRECT_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

MB_OK = 0x0
MB_ICONWARNING = 0x30


def _reg_value(hive, subkey, value):
    try:
        with winreg.OpenKey(hive, subkey) as k:
            return winreg.QueryValueEx(k, value)[0]
    except Exception:
        return None


def is_webview2_available():
    """检测本机是否具备运行 WebView2 的条件：独立运行时 或 Edge 浏览器（加载器可回退使用）。"""
    # 1) 独立 WebView2 运行时（常青版），注册在 EdgeUpdate 下
    rt_guid = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for base in (r"SOFTWARE", r"SOFTWARE\WOW6432Node"):
        if _reg_value(winreg.HKEY_LOCAL_MACHINE, base + "\\" + rt_guid, "pv"):
            return True
    # 2) 已安装 Microsoft Edge 浏览器（WebView2 加载器会回退使用它）
    for base in (r"SOFTWARE\Microsoft\Edge", r"SOFTWARE\WOW6432Node\Microsoft\Edge"):
        if _reg_value(winreg.HKEY_LOCAL_MACHINE, base, "pv"):
            return True
    # 3) 直接探测 Edge 可执行文件
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(p):
            return True
    return False


def show_native_message(title, text):
    """弹出一个系统原生消息框（frozen --noconsole 下依然可用）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, MB_OK | MB_ICONWARNING)
    except Exception:
        print(text)


def open_url(url):
    """用系统默认方式打开链接，优先 webbrowser，失败回退到 ShellExecute。"""
    try:
        webbrowser.open(url)
    except Exception:
        try:
            ctypes.windll.shell32.ShellExecuteW(0, "open", url, None, None, 1)
        except Exception:
            pass


def require_webview2():
    """缺失 WebView2 时提示并打开下载页，然后退出。"""
    if is_webview2_available():
        return
    title = "计分大屏 - 需要 WebView2"
    text = (
        "本程序需要 Microsoft Edge WebView2 运行时才能显示界面。\n\n"
        "检测到当前电脑尚未安装该组件，无法正常启动。\n\n"
        "请安装 WebView2 运行时（常青版，约几分钟即可完成）：\n"
        + WEBVIEW2_DOWNLOAD_URL
        + "\n\n已自动为你打开官方下载页面；若未弹出，请手动复制上方链接到浏览器。\n\n"
        "安装完成后，重新双击本程序即可正常使用。"
    )
    open_url(WEBVIEW2_DOWNLOAD_URL)
    show_native_message(title, text)
    sys.exit(1)


def read_sidecar():
    try:
        if os.path.exists(SIDECAR):
            with open(SIDECAR, "r", encoding="utf-8") as f:
                return f.read().strip()
        # 兼容旧版文件名，首次运行自动沿用已有数据
        if os.path.exists(OLD_SIDECAR):
            with open(OLD_SIDECAR, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def write_sidecar(text):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SIDECAR + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, SIDECAR)  # 原子写，避免半截文件
    # 迁移：写入新文件后清理旧版数据文件
    try:
        if (
            os.path.exists(OLD_SIDECAR)
            and os.path.abspath(OLD_SIDECAR) != os.path.abspath(SIDECAR)
        ):
            os.remove(OLD_SIDECAR)
    except Exception:
        pass


def build_html():
    with open(os.path.join(BASE, PAGE_FILE), "r", encoding="utf-8") as f:
        html = f.read()
    sidecar = read_sidecar().replace("</", "<\\/")  # 防止 JSON 中的 </script> 截断
    # 移除已有的 embeddedState（若有），避免重复 id
    html = re.sub(
        r'<script[^>]*id="embeddedState"[^>]*>.*?</script>', "", html, flags=re.S
    )
    # 注入到 <body> 之后（保证在末尾主脚本执行前已被解析进 DOM）
    html = re.sub(
        r"(<body[^>]*>)",
        lambda m: m.group(1)
        + '\n<script id="embeddedState" type="application/json">'
        + sidecar
        + "</script>",
        html,
        count=1,
    )
    return html


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = build_html().encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204)
            return
        # 其余静态资源（本应用图标等均为内联，此处兜底 404）
        self._send(404)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/_save":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                text = raw.decode("utf-8")
                json.loads(text)  # 校验确为 JSON
                write_sidecar(text)
                self._send(200, b"ok")
            except Exception as e:
                self._send(400, str(e).encode("utf-8"))
            return
        self._send(404)

    def log_message(self, *args, **kwargs):
        pass


def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    import webview

    # 启动前先确认 WebView2 可用，缺失则提示并引导安装
    require_webview2()

    start_server()

    user_data = os.path.join(DATA_DIR, "webview-data")
    os.makedirs(user_data, exist_ok=True)

    webview.create_window(
        "计分大屏",
        url=f"http://127.0.0.1:{PORT}/index.html",
        width=1366,
        height=768,
        resizable=True,
        background_color="#0f1115",
    )
    # storage_path: 把 WebView2 配置文件固定在 exe 同级目录（便于整体拷贝）
    # private_mode=False: 允许 localStorage 也持久化（我们的数据主存是侧车 json，这里作双保险）
    try:
        webview.start(storage_path=user_data, private_mode=False)
    except Exception as e:
        msg = str(e)
        # 极个别情况下 start() 仍可能因缺运行时抛错，这里兜底给同样的引导
        if "WebView2" in msg or "WebEngine" in msg:
            open_url(WEBVIEW2_DOWNLOAD_URL)
            show_native_message(
                "计分大屏 - 启动失败",
                "启动失败：缺少 WebView2 运行时。\n请安装后重试：\n"
                + WEBVIEW2_DOWNLOAD_URL,
            )
        else:
            show_native_message("计分大屏 - 启动失败", "启动失败：\n" + msg)
        sys.exit(1)


if __name__ == "__main__":
    main()

# K6 Telemetry Console — Electron

这是 K6 遥测灯效的独立窗口版本。Electron 负责窗口、WebHID 权限与本地桥接进程；内置 Python 桥接读取 AMS2、AC EVO、AC、ACC、LMU、Forza Horizon 5/6 遥测。

## 开发运行

首次从干净仓库运行：

```powershell
pnpm install --frozen-lockfile
pnpm run build:bridge
pnpm run dev
```

`build:bridge` 在缺少 `.venv` 时会自动执行 `setup:bridge`。它需要系统已安装 Python 3；PyInstaller 版本固定在 `requirements-build.txt`。

## 测试与同步

```powershell
pnpm test
pnpm run check:browser
```

Electron 目录是共享运行代码的唯一源。修改 `acevo_bridge.py`、`ams2_bridge.py`、`game_sources.py`、`app.js`、`runtime-core.mjs`、`index.html` 或 `style.css` 后，执行：

```powershell
pnpm run sync:browser
pnpm run check:browser
```

## 打包

```powershell
pnpm run dist:portable
```

输出的 portable EXE 不需要另开 Chrome/Edge；`pnpm run dist` 会同时生成 portable EXE 与安装程序。

## 运行约束

- `0.2.7` 起，桥接服务带随机会话令牌、版本、父/子 PID。端口 `8766` 上的旧版或其他 HTTP 服务不会被误认为本次桥接。
- `0.2.8` 修复了驾驶中普通转速灯覆盖车辆仪表信号的问题：AC EVO 的左右转向、双闪、雨刷、雨灯、大灯、特殊灯和座舱灯，以及 ACC / LMU / AMS2 实际可提供的对应信号，会在发动机转动时正常显示；ABS、刹车和换挡仍优先于这些仪表信号。
- WebHID 权限只授予页面自身来源及 Flydigi K6 的 VID/PID，不再允许任意 HID 设备。
- 连接 K6 后会按 A3 声明长度逐块备份灯光 RAM `0x04`。快照支持 A4 封装 `LGHT`、直接 `LGHT` 和固件内置 opaque 预设，不会再把合法的直接 RAM 内容误判为不可恢复。窗口关闭时先停止新灯效，等待当前灯光事务结束，再原样写回快照并关闭 HID。
- A4 的整段 `BEGIN → 分块 → COMMIT/ABORT` 由同一事务队列串行化，关闭恢复不会插进正在进行的灯光写入。
- HID 回包优先按声明长度解析；部分 K6 接收器使用命令特定长度时，仅在校验和正确且后续全为 HID 零填充的条件下启用兼容解析。
- 核心优先级严格为 `ABS > 刹车 > 换挡 > 转速 > TC > 逆向 > DRS`。刹车闪烁会随踏板压力连续加快。
- AC/ACC 的原始挡位已归一化为 `R=-1、N=0、1挡=1`；页面和其他游戏使用同一语义。
- AC EVO 会区分驾驶、回放和展厅。非驾驶状态不会把静止共享内存误判为 ABS、TC、转速等驾驶警告；展厅仍可显示可取得的车辆灯光、雨刷等设备状态。
- Electron 禁用后台与遮挡节流，ACC 独占全屏覆盖窗口时仍保持遥测轮询、闪烁时钟和 HID 输出。

首次使用时，在窗口中点击“连接 K6”。AMS2 需要在游戏中启用 `Project CARS 2` Shared Memory；ACC 在进入驾驶界面后自动创建共享内存；FH5/FH6 需要把 UDP Data Out 指向 `127.0.0.1:5300`。

项目以 MIT License 发布，详见 `LICENSE`。

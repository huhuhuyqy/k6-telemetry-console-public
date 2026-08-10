# K6 Telemetry Console — Electron

这是浏览器版 K6 遥测 Demo 的独立窗口版本。Electron 负责窗口和 WebHID 权限，内置桥接进程负责读取 AMS2 / AC EVO / AC / ACC / LMU / FH5 / FH6 数据。

## 开发运行

先安装依赖，然后执行：

```powershell
pnpm install
pnpm run build:bridge
pnpm run dev
```

## 打包

```powershell
pnpm run dist:portable
```

输出的 portable EXE 不需要另外打开 Chrome/Edge。`pnpm run dist` 会同时生成 portable EXE 和安装程序。

`0.2.4` 在连接 K6 后会先用 A3 读取并保存灯光 RAM `0x04` 的完整原始配置。点击窗口关闭按钮时，Electron 会等待页面停止遥测灯效、用 A4 原样写回该快照并关闭 HID，然后才结束桥接进程和主窗口。若首次快照读取失败，程序会进入保护模式，不再覆盖手柄灯效。

首次使用时，在窗口中点击“连接 K6”。AMS2 仍需在游戏中开启 Project CARS 2 Shared Memory；ACC PC 版会在进入驾驶界面后自动提供共享内存，无需额外开关；FH5/FH6 仍需配置 UDP Data Out。

选择“手动模拟器”可独立测试 AC EVO 灯效，包括 ABS、TC、升降挡、DRS、进站限速、转向与双闪、大灯与雨刷、车辆警告、赛道旗帜、损伤、出界、温度和 ERS 状态。多个状态同时开启时使用与实时遥测相同的优先级。

菜单、车辆展厅和车辆静止时的默认零值不会触发警告。圈速无效仅在行驶中检测到“有效→无效”变化后短暂提示，避免告警长时闪烁。

Electron 已禁用后台与窗口遮挡节流，游戏使用独占全屏时仍会保持遥测轮询、闪烁时钟和 HID 写入频率。Forza 读取器支持 232/311/323/324/331 字节格式，并会将轮胎温度从华氏度转为摄氏度。

# Tasks

- [x] Task 1: 创建 Spotify 插件骨架（plugin-info.json、icon.png、目录结构）
  - [x] SubTask 1.1: 创建 `src/plugins/spotify_web/plugin-info.json`，定义 `id: "spotify_web"`、`class: "SpotifyWeb"`、`display_name: "Spotify Web Player"`
  - [x] SubTask 1.2: 创建 `src/plugins/spotify_web/icon.png`（Spotify 风格图标）

- [x] Task 2: 实现 `SpotifyWeb` 插件类（`src/plugins/spotify_web/spotify_web.py`）
  - [x] SubTask 2.1: 继承 `BasePlugin`，实现 `generate_image()` 返回占位图（"Spotify Web Player 启动中..."）
  - [x] SubTask 2.2: 实现 `cleanup()` 方法，终止 Chromium 与 Xorg 进程
  - [x] SubTask 2.3: 实现 `generate_settings_template()`，隐藏 style_settings 与 refresh_interval
  - [x] SubTask 2.4: 实现进程管理辅助方法：`_start_chromium()`、`_stop_chromium()`、`_is_running()`

- [x] Task 3: 实现插件设置页（`src/plugins/spotify_web/settings.html`）
  - [x] SubTask 3.1: 用户名输入框（回填已保存值）
  - [x] SubTask 3.2: 密码输入框（掩码，不回显明文）+ 保存按钮
  - [x] SubTask 3.3: 「启动 Web 播放器」/「停止 Web 播放器」按钮 + 运行状态指示
  - [x] SubTask 3.4: 蓝牙音频设备状态区（复用 `/bluetooth/status` 端点）
  - [x] SubTask 3.5: JS 逻辑：保存凭据、启停播放器、轮询运行状态

- [x] Task 4: 在 `src/blueprints/plugin.py` 新增 Spotify 专用端点
  - [x] SubTask 4.1: `GET /plugin/spotify_web/credentials` — 返回用户名与密码是否已设置（不返回密码明文）
  - [x] SubTask 4.2: `POST /plugin/spotify_web/credentials` — 保存用户名到 device config，密码写入 `.env`
  - [x] SubTask 4.3: `POST /plugin/spotify_web/start` — 启动 Xorg + Chromium kiosk 进程
  - [x] SubTask 4.4: `POST /plugin/spotify_web/stop` — 终止 Chromium + Xorg 进程
  - [x] SubTask 4.5: `GET /plugin/spotify_web/status` — 返回播放器运行状态（running/pid）

- [x] Task 5: 实现 Chromium kiosk 启动逻辑
  - [x] SubTask 5.1: 使用 `xinit` 启动最小 X server，DISPLAY=:0
  - [x] SubTask 5.2: Chromium 启动参数：`--kiosk --noerrdialogs --disable-translate --autoplay-policy=no-user-gesture-required --user-data-dir=<persistent_dir> https://open.spotify.com/`
  - [x] SubTask 5.3: 持久化用户数据目录：`src/static/spotify_profile/`，添加到 `.gitignore`
  - [x] SubTask 5.4: 注入登录自动化 JS（通过 Chromium unpacked extension），预填用户名/密码并提交登录表单
  - [x] SubTask 5.5: 确保非树莓派环境（dev mode）下优雅降级，返回错误提示而非崩溃

- [x] Task 6: 蓝牙音频路由配置
  - [x] SubTask 6.1: 启动 Chromium 前调用 `pactl` 设置已连接蓝牙音频设备为默认 sink
  - [x] SubTask 6.2: 在设置页显示当前蓝牙音频设备连接状态

- [x] Task 7: 更新依赖与 .gitignore
  - [x] SubTask 7.1: 在 `install/debian-requirements.txt` 新增 `chromium-browser`、`xserver-xorg`、`xinit`、`pulseaudio-module-bluetooth`
  - [x] SubTask 7.2: 在 `.gitignore` 新增 `src/static/spotify_profile/`

# Task Dependencies
- Task 2 依赖 Task 1（需要 plugin-info.json）
- Task 3 依赖 Task 4（需要端点存在才能调用）
- Task 4 依赖 Task 2（端点调用插件方法）
- Task 5 依赖 Task 4（端点触发启动逻辑）
- Task 6 依赖 Task 5（在 Chromium 启动前配置音频）
- Task 7 可与 Task 1-6 并行

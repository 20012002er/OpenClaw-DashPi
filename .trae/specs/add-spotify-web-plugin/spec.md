# Spotify Web Player 插件 Spec

## Why
用户希望在树莓派连接的 7 寸触摸屏上直接显示并操作 Spotify Web 播放器（https://open.spotify.com/），通过蓝牙音频设备播放音乐。需要在 8080 后管页面中配置 Spotify 账号信息，使触摸屏上启动播放器时自动带上用户凭据登录，无需在触摸屏上手动输入。

## What Changes
- 新增 `spotify_web` 插件目录（`src/plugins/spotify_web/`），包含 `spotify_web.py`、`plugin-info.json`、`settings.html`、`icon.png`
- 新增插件类 `SpotifyWeb`，继承 `BasePlugin`，实现 `generate_image()` 返回占位图（"Spotify Web Player 启动中..."），并在插件激活时启动 Chromium kiosk 进程接管显示
- 插件 `cleanup()` 方法在插件停用/切换时终止 Chromium 与 Xorg 进程，恢复 framebuffer 直接访问
- 在 `src/blueprints/plugin.py` 新增 Spotify 专用端点：启动/停止 Web 播放器、保存/读取 Spotify 用户凭据
- Spotify 用户名存储于 device config（`spotify_username`），密码存储于 `.env`（`SPOTIFY_PASSWORD`），遵循项目「API keys must be stored in .env」约束
- 插件设置页（`settings.html`）提供：用户名/密码输入、Web 播放器启停按钮、蓝牙音频设备状态展示（复用现有 `/bluetooth/*` 端点）
- Chromium 使用持久化用户数据目录（`src/static/spotify_profile`），首次登录后会话 cookie 持久化，后续启动自动登录
- Chromium 启动参数包含 `--kiosk`、`--noerrdialogs`、`--disable-translate`、`--autoplay-policy=no-user-gesture-required`，并注入登录自动化 JS（预填用户名/密码并提交）
- 新增依赖：`xinit`、`xserver-xorg`、`chromium-browser`（写入 `install/debian-requirements.txt`）
- 蓝牙音频路由：启动播放器前确保 PulseAudio 已加载蓝牙模块，并设置已连接的蓝牙音频设备为默认 sink

## Impact
- Affected specs: 新增插件能力；不影响现有插件
- Affected code:
  - 新增 `src/plugins/spotify_web/`（spotify_web.py、plugin-info.json、settings.html、icon.png）
  - 修改 `src/blueprints/plugin.py`（新增 Spotify 端点）
  - 修改 `install/debian-requirements.txt`（新增 chromium、xorg 依赖）
  - 修改 `src/dashpi.py` 的 CSP 头以允许嵌入 Spotify web 播放器相关资源（仅在该插件页面）
  - 复用 `src/utils/bluetooth_manager.py` 的蓝牙连接能力
  - 复用 `src/blueprints/apikeys.py` 的 .env 写入模式

## ADDED Requirements

### Requirement: Spotify Web 播放器显示
系统 SHALL 在 Spotify 插件被激活（pin 或 Update Now）时，在树莓派 7 寸触摸屏上启动 Chromium kiosk 模式，加载 `https://open.spotify.com/`。

#### Scenario: 首次启动播放器
- **WHEN** 用户在 8080 后管页面点击「启动 Web 播放器」按钮
- **THEN** 系统启动 Xorg 与 Chromium kiosk 进程，触摸屏显示 Spotify Web 播放器
- **AND** 触摸屏上可交互操作（点击、滚动、播放控制）

#### Scenario: 停止播放器
- **WHEN** 用户点击「停止 Web 播放器」按钮，或切换到其他插件
- **THEN** 系统终止 Chromium 与 Xorg 进程
- **AND** 恢复 framebuffer 直接访问，显示其他插件图像

### Requirement: Spotify 凭据管理
系统 SHALL 在 8080 后管的 Spotify 插件设置页提供用户名与密码输入框，密码存储于 `.env` 文件。

#### Scenario: 保存凭据
- **WHEN** 用户在设置页输入用户名和密码并点击保存
- **THEN** 用户名写入 device config（`spotify_username`）
- **AND** 密码写入 `.env` 文件（`SPOTIFY_PASSWORD`），不回显明文
- **AND** 返回成功响应

#### Scenario: 读取凭据状态
- **WHEN** 用户打开 Spotify 插件设置页
- **THEN** 用户名回填到输入框
- **AND** 密码框显示为已设置状态（掩码），不回显明文

### Requirement: 自动登录
系统 SHALL 在 Chromium 启动时，使用持久化用户数据目录与注入的登录脚本，自动登录 Spotify 账号。

#### Scenario: 已有会话 cookie
- **WHEN** Chromium 启动且持久化目录中存在有效会话 cookie
- **THEN** 直接显示已登录的 Web 播放器，不显示登录页

#### Scenario: 无会话 cookie（首次或会话过期）
- **WHEN** Chromium 启动且无有效会话 cookie
- **AND** 用户已配置用户名和密码
- **THEN** 注入的 JS 自动填充登录表单并提交
- **AND** 登录成功后显示 Web 播放器

#### Scenario: 未配置凭据
- **WHEN** Chromium 启动且无有效会话 cookie
- **AND** 用户未配置密码
- **THEN** 显示 Spotify 登录页，等待用户在触摸屏上手动登录
- **AND** 登录后会话 cookie 持久化到用户数据目录

### Requirement: 蓝牙音频播放
系统 SHALL 在 Web 播放器运行期间，通过已连接的蓝牙音频设备输出音频。

#### Scenario: 蓝牙音频设备已连接
- **WHEN** 启动 Web 播放器且已有蓝牙音频设备连接
- **THEN** PulseAudio 默认 sink 设置为该蓝牙设备
- **AND** Spotify 播放的音乐通过蓝牙设备输出

#### Scenario: 无蓝牙音频设备
- **WHEN** 启动 Web 播放器且无蓝牙音频设备连接
- **THEN** 在设置页显示提示「未连接蓝牙音频设备」
- **AND** 音频通过默认音频输出（3.5mm/HDMI）

### Requirement: 插件集成
系统 SHALL 将 Spotify 插件注册到插件系统中，遵循现有插件架构（plugin-info.json、BasePlugin 子类）。

#### Scenario: 插件出现在插件列表
- **WHEN** 用户访问 8080 后管首页
- **THEN** 插件网格中显示「Spotify Web Player」卡片

#### Scenario: 插件设置页
- **WHEN** 用户点击 Spotify 插件卡片
- **THEN** 显示 Spotify 插件设置页，包含凭据输入、启停按钮、蓝牙状态

# Screen Time Tracker / 屏幕时间追踪器

## English

A lightweight Windows application that monitors Caps Lock status and tracks screen time usage with a visual timeline.

### Features
- **Caps Lock Status Display**: Real-time monitoring of Caps Lock state with color-coded indicators (Red for ON, Green for OFF)
- **Countdown Timer**: Built-in 25-minute countdown timer (configurable) with start/pause/reset controls
- **Screen Time Tracking**: Automatically tracks time spent on different applications
- **Visual Timeline**: Displays usage history as colored pixel blocks
- **Statistics Panel**: Shows usage statistics with horizontal bar charts
- **Always on Top**: Window stays visible above other applications
- **Borderless Design**: Clean, modern interface with auto-hide title bar

### Usage
1. Run `stt_new.exe`
2. The window will appear in the center of the screen
3. Move mouse to the top to show the title bar
4. Right-click for settings menu
5. Click the stats button (📊) to view usage statistics

### Configuration
Edit `config.txt` to customize:
- `color_caps_on`: Color when Caps Lock is ON (default: #fa6666)
- `color_caps_off`: Color when Caps Lock is OFF (default: #4CAF50)
- `timer_duration_minutes`: Countdown duration in minutes (default: 25)
- `window_width` / `window_height`: Window size
- `always_on_top`: Keep window on top (true/false)

---

## 中文

一款轻量级Windows应用程序，用于监控Caps Lock状态并以可视化时间线追踪屏幕使用时间。

### 功能特性
- **Caps Lock状态显示**: 实时监控Caps Lock状态，使用颜色编码指示器（红色表示开启，绿色表示关闭）
- **倒计时器**: 内置25分钟倒计时器（可配置），带有开始/暂停/重置控制
- **屏幕时间追踪**: 自动追踪在不同应用程序上花费的时间
- **可视化时间线**: 以彩色像素块显示使用历史
- **统计面板**: 以水平条形图显示使用统计
- **始终置顶**: 窗口保持在其他应用程序上方可见
- **无边框设计**: 简洁现代的界面，标题栏自动隐藏

### 使用方法
1. 运行 `stt_new.exe`
2. 窗口将出现在屏幕中央
3. 将鼠标移到顶部以显示标题栏
4. 右键点击打开设置菜单
5. 点击统计按钮 (📊) 查看使用统计

### 配置说明
编辑 `config.txt` 进行自定义：
- `color_caps_on`: Caps Lock开启时的颜色（默认：#fa6666）
- `color_caps_off`: Caps Lock关闭时的颜色（默认：#4CAF50）
- `timer_duration_minutes`: 倒计时时长（分钟，默认：25）
- `window_width` / `window_height`: 窗口大小
- `always_on_top`: 保持窗口置顶（true/false）

# 屏幕时间追踪器 v1.12.2 - README

## 中文版

### 简介

屏幕时间追踪器是一款专为Windows系统设计的轻量级桌面应用程序，用于监控和记录用户的屏幕使用时间。该应用程序能够实时跟踪活动窗口，记录每个应用程序的使用时间，并通过直观的界面展示使用统计信息。

### 功能特点

- **实时监控**：持续跟踪用户的活动窗口，记录每个应用程序的使用时间
- **大小写状态指示**：通过颜色变化直观显示大小写锁定键的状态（绿色表示关闭，红色表示开启）
- **时间流可视化**：底部时间流以像素块形式展示最近的使用记录，每个像素块代表一定时间间隔
- **统计功能**：提供详细的使用统计，包括各应用程序使用时长和百分比
- **自定义配置**：支持通过配置文件自定义颜色、时间间隔等设置

### 系统要求

- Windows 7/8/10/11
- 至少50MB可用磁盘空间
- 屏幕分辨率不低于1024×768

### 安装与使用

1. 解压下载的压缩包
2. 双击`启动屏幕时间追踪器_新版.bat`文件运行应用程序
3. 或者直接双击`stt_new_1122.exe`文件运行
4. 应用程序将在系统托盘区域运行，开始记录屏幕使用时间

### 配置说明

应用程序的配置文件为`config.txt`，可以自定义以下设置：

- `color_caps_on`：大小写开启时的背景色（默认为#fa6666）
- `color_caps_off`：大小写关闭时的背景色（默认为#4CAF50）
- `time_stream_pixel_seconds`：时间流每个像素代表的秒数（默认为60）
- `check_interval`：检查活动窗口的间隔时间（毫秒）

### 数据存储

应用程序使用SQLite数据库存储使用记录：
- 数据库文件：`screen_time_history.db`
- 日志文件：存储在`logs`目录下
- 数据自动备份机制确保数据安全

### 故障排除

如果程序无法正常运行，请尝试以下解决方案：

1. **权限问题**：以管理员身份运行程序
2. **杀毒软件**：将程序添加到杀毒软件白名单
3. **数据库损坏**：删除`screen_time_history.db`文件，程序会自动创建新的数据库
4. **显示问题**：检查屏幕分辨率是否符合最低要求

### 更新日志

#### v1.12.2 (2025-11-22)
- 修复了大小写切换时底部时间流背景色同步更新问题
- 打包后的应用程序不再显示命令行窗口
- 使用了新的图标文件
- 优化了内存使用和性能


## English Version

### Introduction

Screen Time Tracker is a lightweight desktop application designed for Windows systems to monitor and record users' screen time. The application can track active windows in real-time, record usage time for each application, and display usage statistics through an intuitive interface.

### Features

- **Real-time Monitoring**: Continuously tracks the user's active windows and records the usage time for each application
- **Caps Lock Status Indicator**: Visually displays the status of the Caps Lock key through color changes (green for off, red for on)
- **Time Stream Visualization**: The bottom time stream displays recent usage records in pixel blocks, with each block representing a specific time interval
- **Statistics Function**: Provides detailed usage statistics, including duration and percentage for each application
- **Customizable Configuration**: Supports customization of colors, time intervals, and other settings through configuration files

### System Requirements

- Windows 7/8/10/11
- At least 50MB of available disk space
- Screen resolution of 1024×768 or higher

### Installation and Usage

1. Extract the downloaded zip file
2. Double-click the `启动屏幕时间追踪器_新版.bat` file to run the application
3. Or directly double-click the `stt_new_1122.exe` file to run
4. The application will run in the system tray area and start recording screen time

### Configuration

The application's configuration file is `config.txt`, where you can customize the following settings:

- `color_caps_on`: Background color when Caps Lock is on (default: #fa6666)
- `color_caps_off`: Background color when Caps Lock is off (default: #4CAF50)
- `time_stream_pixel_seconds`: Seconds represented by each pixel in the time stream (default: 60)
- `check_interval`: Interval time for checking active windows (milliseconds)

### Data Storage

The application uses SQLite database to store usage records:
- Database file: `screen_time_history.db`
- Log files: Stored in the `logs` directory
- Automatic data backup mechanism ensures data security

### Troubleshooting

If the program cannot run normally, please try the following solutions:

1. **Permission Issues**: Run the program as an administrator
2. **Antivirus Software**: Add the program to the antivirus software whitelist
3. **Database Corruption**: Delete the `screen_time_history.db` file, and the program will automatically create a new database
4. **Display Issues**: Check if the screen resolution meets the minimum requirements

### Changelog

#### v1.12.2 (2025-11-22)
- Fixed the issue where the bottom time stream background color did not update synchronously when switching Caps Lock
- The packaged application no longer displays the command line window
- Used new icon file
- Optimized memory usage and performance

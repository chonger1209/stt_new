# Changelog / 更新日志

## 2026.10.22 优化试用版

- 新版源码独立存放 optimized/，旧入口、旧 EXE 和旧 Release 保留。
- 数据库后台读写，失败保留批次重试，退出等待保存；同一数据目录增加单实例保护。
- 时间流按累计秒数绘格，修复切换应用额外产生格子；倒计时使用单调时钟。
- 完善中文配置与既有设置保留，可选千问管理增加休眠、启动和强退二次确认。
- 发布包仅包含程序、图标、默认配置、说明与服务适配器，不包含个人数据库、日志、密钥或模型。
- 90 项逻辑测试与 8 项模拟接口测试通过，真实模型和跨设备验证仍待进行。


## Version 2026.03.12

### English

#### UI Layout Optimization
- **Caps Lock Display**:
  - Reduced font size from 12px to 10px
  - Reduced top padding from 30 to 10
  - Reduced spacing between "Caps Lock" and "ON/OFF" from 2 to 1

- **Countdown Timer**:
  - Reduced font size from 32px to 24px
  - Reduced top padding to move timer higher
  - Reduced spacing between timer and buttons
  - Reduced bottom padding of button container

- **Button Optimization**:
  - Reduced font size from 10px to 9px
  - Adjusted button padding to ensure square shape
  - Reduced spacing between buttons from 3 to 2

#### Space Allocation Optimization
- Reduced overall height to provide more space for the timeline
- More compact layout with better visual aesthetics

#### Bug Fixes
- Fixed countdown position being too low
- Fixed inconsistent button sizes and shapes
- Fixed unreasonable overall layout space allocation

---

### 中文

#### 界面布局优化
- **CAPS lock显示**：
  - 字体大小从12px减小到10px
  - 顶部间距从30减小到10
  - "Caps Lock"和"ON/OFF"之间的间距从2减小到1

- **倒计时功能**：
  - 字体大小从32px减小到24px
  - 顶部间距减小，使倒计时更靠上
  - 倒计时与按钮之间的间距减小
  - 按钮容器的底部间距减小

- **按钮优化**：
  - 字体大小从10px减小到9px
  - 按钮内边距调整，确保按钮为正方形
  - 按钮之间的间距从3减小到2

#### 空间分配优化
- 整体高度减小，为时间流腾出更多空间
- 布局更加紧凑，视觉效果更佳

#### 问题修复
- 修复倒计时位置过于靠下的问题
- 修复按钮大小和形状不一致的问题
- 修复整体布局空间分配不合理的问题

---

## Previous Versions / 历史版本

### Version 2025.11.22
- Initial release with basic Caps Lock monitoring
- Added screen time tracking functionality
- Implemented visual timeline with pixel blocks

### Version 2025.11.25
- Optimized memory usage and performance
- Fixed pixel grid rendering issues
- Improved database handling

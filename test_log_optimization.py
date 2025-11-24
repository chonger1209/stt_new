#!/usr/bin/env python3
"""
测试脚本：验证时间流像素格日志优化
"""
import os
import time
import subprocess
import sys

def test_log_optimization():
    """测试日志优化功能"""
    print("测试时间流像素格日志优化...")
    
    # 检查程序是否存在
    exe_path = "release_clean/stt_new_1122.exe"
    if not os.path.exists(exe_path):
        print(f"错误：找不到程序文件 {exe_path}")
        return False
    
    # 创建测试日志目录
    log_dir = "release_clean/logs"
    os.makedirs(log_dir, exist_ok=True)
    
    # 启动程序
    print("启动程序...")
    process = subprocess.Popen([exe_path])
    
    # 等待程序启动
    time.sleep(3)
    
    # 检查日志文件
    log_files = [f for f in os.listdir(log_dir) if f.startswith("log_")]
    if not log_files:
        print("警告：未找到日志文件")
    else:
        # 按修改时间排序，获取最新的日志文件
        log_files.sort(key=lambda x: os.path.getmtime(os.path.join(log_dir, x)), reverse=True)
        latest_log = os.path.join(log_dir, log_files[0])
        
        print(f"检查日志文件：{latest_log}")
        
        # 读取日志内容
        with open(latest_log, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 统计像素格日志
        pixel_logs = [line for line in lines if "绘制时间流像素格" in line]
        new_pixel_logs = [line for line in lines if "新增像素格数" in line]
        
        print(f"总日志行数：{len(lines)}")
        print(f"像素格日志行数：{len(pixel_logs)}")
        print(f"包含新增像素格数的日志行数：{len(new_pixel_logs)}")
        
        if new_pixel_logs:
            print("最新的一条新增像素格日志：")
            print(new_pixel_logs[-1].strip())
        
        # 验证优化是否生效
        if len(new_pixel_logs) > 0:
            print("\n✓ 日志优化已生效：程序记录了新增像素格的数量")
        else:
            print("\n✗ 日志优化可能未生效：未找到新增像素格数量的日志")
    
    # 等待用户确认
    input("\n按Enter键关闭测试程序...")
    
    # 关闭程序
    process.terminate()
    process.wait()
    
    print("测试完成")
    return True

if __name__ == "__main__":
    test_log_optimization()
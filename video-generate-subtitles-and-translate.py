import os
import tkinter as tk
from tkinter import filedialog
import whisper
import threading
import time
from pydub import AudioSegment
from langdetect import detect
import requests
import uuid
from tenacity import retry, stop_after_attempt, wait_fixed
import urllib3
from pathlib import Path
import sys

# 全局变量
root = None
status_text = None
translate_var = None
stop_flag = False  # 停止标志
processing_thread = None  # 处理线程
whisper_model = None  # 添加whisper_model全局变量

# 微软翻译API配置
# SUBSCRIPTION_KEY = "你的订阅密钥"  # 替换为你的密钥
# ENDPOINT = "https://api.cognitive.microsofttranslator.com"
# LOCATION = "eastasia"  # 替换为你的区域

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def translate_single_text(text, url, params, headers):
    """单次翻译请求,带有重试机制"""
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    return response.json()['data']['translation']

def translate_text(text):
    """使用Google翻译API翻译文本"""
    if not text:
        return text
        
    url = "https://translate.googleapis.com/translate_a/single"
    
    params = {
        'client': 'gtx',
        'sl': 'auto',  # 源语言自动检测
        'tl': 'zh-CN', # 目标语言：中文
        'dt': 't',     # 只返回翻译结果
        'q': text
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        # 禁用SSL验证，因为某些环境可能有证书问题
        response = requests.get(url, params=params, headers=headers, verify=False, timeout=10)
        
        if response.status_code == 200:
            # Google翻译API返回的是一个嵌套列表，我们需要提取第一个翻译结果
            result = response.json()
            translation = ''.join(item[0] for item in result[0] if item[0])
            
            # 增加延时，避免请求过快
            time.sleep(1.5)
            
            return translation
        else:
            update_status(f"翻译请求失败，状态码: {response.status_code}，将使用原文")
            return text
            
    except requests.exceptions.Timeout:
        update_status("翻译请求超时，将使用原文")
        return text
    except requests.exceptions.RequestException as e:
        update_status(f"翻译请求失败: {str(e)}，将使用原文")
        return text
    except Exception as e:
        update_status(f"翻译发生错误: {str(e)}，将使用原文")
        return text

# 只初始化一次 Whisper 模型
def init_whisper_model():
    """初始化Whisper模型"""
    global whisper_model
    try:
        if whisper_model is None:
            update_status("正在加载Whisper模型...")
            whisper_model = whisper.load_model("base")
            update_status("Whisper模型加载完成")
        return True
    except Exception as e:
        update_status(f"加载Whisper模型失败: {str(e)}")
        return False

# 获取音频文件时长
def get_audio_duration(audio_file):
    try:
        audio = AudioSegment.from_file(audio_file)
        return len(audio) / 1000.0  # 时长（秒）
    except Exception as e:
        update_status(f"加载音频时出错: {str(e)}")
        return 0

# 使用 Whisper 模型识别音频
def recognize_audio_whisper(audio_file):
    start_time = time.time()
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
    update_status(f"开始识别音频... ({current_time})")
    try:
        # 确保模型已加载
        if not init_whisper_model():
            return None
            
        result = whisper_model.transcribe(audio_file, word_timestamps=True)
        end_time = time.time()
        elapsed_time = end_time - start_time
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        update_status(f"音频识别完成！({current_time})")
        update_status(f"识别耗时: {elapsed_time:.2f} 秒")
        return result
    except Exception as e:
        update_status(f"音频识别失败: {str(e)}")
        return None

# 格式化时间为 SRT 格式
def format_time(seconds):
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{int(hours):02}:{int(minutes):02}:{int(secs):02},{millis:03}"

# 生成 SRT 字幕文件
def generate_srt(subtitles, output_file):
    with open(output_file, "w", encoding="utf-8") as file:
        for idx, subtitle in enumerate(subtitles):
            start_time = format_time(subtitle['start'])
            end_time = format_time(subtitle['end'])
            file.write(f"{idx + 1}\n")
            file.write(f"{start_time} --> {end_time}\n")
            file.write(f"{subtitle['text']}\n\n")

# 翻译 SRT 字幕文件
def translate_srt_file(file_path):
    global stop_flag
    stop_flag = False
    start_time = time.time()
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
    update_status(f"开始翻译字幕文件... ({current_time})")
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            lines = file.readlines()
        
        translated_lines = lines.copy()
        # 计算实际需要翻译的行数（排除序号、时间轴和空行）
        text_lines = [i for i, line in enumerate(lines) 
                     if line.strip() and "-->" not in line and not line.strip().isdigit()]
        total_lines = len(text_lines)
        translated_count = 0
        failed_translations = []
        
        for i in text_lines:
            if stop_flag:
                break
            
            # 显示翻译进度
            translated_count += 1
            progress = (translated_count / total_lines) * 100
            update_status(f"翻译进度: {progress:.1f}% ({translated_count}/{total_lines})")
            
            # 尝试翻译，如果失败则保存原文
            translated_text = translate_text(lines[i].strip())
            if translated_text == lines[i].strip():
                failed_translations.append(i + 1)
            translated_lines[i] = translated_text + "\n"
            
            # 每翻译10行保存一次，避免全部丢失
            if translated_count % 10 == 0:
                temp_output_file = os.path.splitext(file_path)[0] + "_翻译_temp.srt"
                with open(temp_output_file, "w", encoding="utf-8") as temp_file:
                    temp_file.writelines(translated_lines)

        if not stop_flag:
            output_file = os.path.splitext(file_path)[0] + "_翻译.srt"
            with open(output_file, "w", encoding="utf-8") as file:
                file.writelines(translated_lines)
            
            # 删除临时文件
            temp_output_file = os.path.splitext(file_path)[0] + "_翻译_temp.srt"
            if os.path.exists(temp_output_file):
                os.remove(temp_output_file)
            
            end_time = time.time()
            elapsed_time = end_time - start_time
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            update_status(f"翻译完成！({current_time})")
            update_status(f"翻译耗时: {elapsed_time:.2f} 秒")
            update_status(f"总共翻译了 {total_lines} 条字幕")
            
            if failed_translations:
                update_status(f"注意：第 {', '.join(map(str, failed_translations))} 行翻译失败，保留原文")
        else:
            update_status("翻译已停止")
            
    except Exception as e:
        update_status(f"翻译失败: {str(e)}")
        stop_flag = True

# 处理音频文件
def process_audio_file(file_path):
    start_time = time.time()
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
    update_status(f"开始处理音频文件... ({current_time})")
    try:
        result = recognize_audio_whisper(file_path)
        if not result:
            return
        original_subtitles = [{"start": segment["start"], "end": segment["end"], "text": segment["text"]} 
                            for segment in result["segments"]]

        base_filename = os.path.splitext(os.path.basename(file_path))[0]
        output_dir = os.path.dirname(file_path)
        original_output_file = os.path.join(output_dir, f"{base_filename}.srt")

        generate_srt(original_subtitles, original_output_file)
        update_status(f"原始字幕保存：{original_output_file}")

        if translate_var.get():
            first_segment_text = original_subtitles[0]["text"]
            detected_language = detect(first_segment_text)
            if detected_language != "zh-cn":
                translate_srt_file(original_output_file)
            else:
                update_status("字幕已是中文，无需翻译。")

        end_time = time.time()
        elapsed_time = end_time - start_time
        update_status(f"处理完成！总耗时: {elapsed_time:.2f} 秒")
    except Exception as e:
        update_status(f"处理失败: {str(e)}")

# 在独立线程中启动处理
def run_processing_thread(func, *args):
    thread = threading.Thread(target=func, args=args)
    thread.daemon = True
    thread.start()

# 界面相关函数
def open_audio_file():
    file_path = filedialog.askopenfilename(
        title="选择音频/视频文件", 
        filetypes=[("音频/视频文件", "*.mp3;*.mp4;*.wav;*.mkv;*.flac")])
    if file_path:
        # 显示文件信息
        file_size = Path(file_path).stat().st_size / (1024*1024)  # 转换为MB
        duration = get_audio_duration(file_path)
        update_status(f"已选择文件: {file_path}")
        update_status(f"文件大小: {file_size:.2f}MB")
        update_status(f"音频时长: {int(duration//60)}分{int(duration%60)}秒")
        update_status("正在处理请稍候...")
        run_processing_thread(process_audio_file, file_path)

def open_srt_file():
    file_path = filedialog.askopenfilename(
        title="选择字幕文件", 
        filetypes=[("SRT 文件", "*.srt")])
    if file_path:
        # 显示文件信息
        file_size = Path(file_path).stat().st_size / 1024  # 转换为KB
        update_status(f"已选择字幕文件: {file_path}")
        update_status(f"文件大小: {file_size:.2f}KB")
        update_status("正在翻译字幕文件，请稍候...")
        run_processing_thread(translate_srt_file, file_path)

def update_status(status_message):
    if status_text:
        root.after(0, lambda: status_text.insert(tk.END, status_message + "\n"))
        root.after(0, lambda: status_text.see(tk.END))

def stop_processing():
    global stop_flag
    stop_flag = True
    update_status("在停止处理...")

def create_main_window():
    global root, status_text, translate_var
    
    root = tk.Tk()
    root.title("视频生成字幕并翻译")
    root.geometry("500x400")

    # 设置图标
    if getattr(sys, 'frozen', False):
        # 打包后的路径
        icon_path = Path(sys._MEIPASS) / "app.ico"
    else:
        # 开发环境路径
        icon_path = Path(__file__).parent / "app.ico"
    
    if icon_path.exists():
        root.iconbitmap(icon_path)

    # 按钮框架
    button_frame = tk.Frame(root)
    button_frame.pack(pady=10)
    
    tk.Button(button_frame, text="处理音频/视频文件", command=open_audio_file).pack(side=tk.LEFT, padx=5)
    tk.Button(button_frame, text="翻译字幕文件", command=open_srt_file).pack(side=tk.LEFT, padx=5)
    tk.Button(button_frame, text="停止处理", command=stop_processing).pack(side=tk.LEFT, padx=5)

    translate_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="执行字幕翻译", variable=translate_var).pack(pady=5)

    # 状态文本框
    status_text = tk.Text(root, wrap="word", height=15)
    status_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # 滚动条
    scrollbar = tk.Scrollbar(root, command=status_text.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    status_text.config(yscrollcommand=scrollbar.set)

    root.mainloop()

if __name__ == "__main__":
    # 在文件开头添加以下代码，禁用SSL警告
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    create_main_window()

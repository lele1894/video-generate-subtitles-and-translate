import os
import whisper
import time
from pydub import AudioSegment
from langdetect import detect
import requests
import argparse
from tenacity import retry, stop_after_attempt, wait_fixed
import urllib3
from pathlib import Path

# 全局变量
whisper_model = None  # whisper模型全局变量
stop_flag = False    # 停止标志

def print_status(message):
    """打印状态信息"""
    current_time = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] {message}")

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
        response = requests.get(url, params=params, headers=headers, verify=False, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            translation = ''.join(item[0] for item in result[0] if item[0])
            time.sleep(1.5)  # 避免请求过快
            return translation
        else:
            print_status(f"翻译请求失败，状态码: {response.status_code}，将使用原文")
            return text
            
    except Exception as e:
        print_status(f"翻译发生错误: {str(e)}，将使用原文")
        return text

def init_whisper_model():
    """初始化Whisper模型"""
    global whisper_model
    try:
        if whisper_model is None:
            print_status("正在加载Whisper模型...")
            whisper_model = whisper.load_model("base")
            print_status("Whisper模型加载完成")
        return True
    except Exception as e:
        print_status(f"加载Whisper模型失败: {str(e)}")
        return False

def get_audio_duration(audio_file):
    """获取音频文件时长"""
    try:
        audio = AudioSegment.from_file(audio_file)
        return len(audio) / 1000.0  # 时长（秒）
    except Exception as e:
        print_status(f"加载音频时出错: {str(e)}")
        return 0

def recognize_audio_whisper(audio_file):
    """使用Whisper模型识别音频"""
    start_time = time.time()
    print_status("开始识别音频...")
    try:
        if not init_whisper_model():
            return None
            
        result = whisper_model.transcribe(audio_file, word_timestamps=True)
        elapsed_time = time.time() - start_time
        print_status(f"音频识别完成！耗时: {elapsed_time:.2f} 秒")
        return result
    except Exception as e:
        print_status(f"音频识别失败: {str(e)}")
        return None

def format_time(seconds):
    """格式化时间为SRT格式"""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{int(hours):02}:{int(minutes):02}:{int(secs):02},{millis:03}"

def generate_srt(subtitles, output_file):
    """生成SRT字幕文件"""
    with open(output_file, "w", encoding="utf-8") as file:
        for idx, subtitle in enumerate(subtitles):
            start_time = format_time(subtitle['start'])
            end_time = format_time(subtitle['end'])
            file.write(f"{idx + 1}\n")
            file.write(f"{start_time} --> {end_time}\n")
            file.write(f"{subtitle['text']}\n\n")

def translate_srt_file(file_path):
    """翻译SRT字幕文件"""
    global stop_flag
    stop_flag = False
    start_time = time.time()
    print_status("开始翻译字幕文件...")
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            lines = file.readlines()
        
        translated_lines = lines.copy()
        text_lines = [i for i, line in enumerate(lines) 
                     if line.strip() and "-->" not in line and not line.strip().isdigit()]
        total_lines = len(text_lines)
        translated_count = 0
        failed_translations = []
        
        for i in text_lines:
            if stop_flag:
                break
            
            translated_count += 1
            progress = (translated_count / total_lines) * 100
            print_status(f"翻译进度: {progress:.1f}% ({translated_count}/{total_lines})")
            
            translated_text = translate_text(lines[i].strip())
            if translated_text == lines[i].strip():
                failed_translations.append(i + 1)
            translated_lines[i] = translated_text + "\n"

        if not stop_flag:
            output_file = os.path.splitext(file_path)[0] + "_翻译.srt"
            with open(output_file, "w", encoding="utf-8") as file:
                file.writelines(translated_lines)
            
            elapsed_time = time.time() - start_time
            print_status(f"翻译完成！耗时: {elapsed_time:.2f} 秒")
            print_status(f"总共翻译了 {total_lines} 条字幕")
            
            if failed_translations:
                print_status(f"注意：第 {', '.join(map(str, failed_translations))} 行翻译失败，保留原文")
        else:
            print_status("翻译已停止")
            
    except Exception as e:
        print_status(f"翻译失败: {str(e)}")
        stop_flag = True

def process_audio_file(file_path, do_translate=True):
    """处理音频文件"""
    start_time = time.time()
    print_status("开始处理音频文件...")
    try:
        # 显示文件信息
        file_size = Path(file_path).stat().st_size / (1024*1024)  # 转换为MB
        duration = get_audio_duration(file_path)
        print_status(f"文件大小: {file_size:.2f}MB")
        print_status(f"音频时长: {int(duration//60)}分{int(duration%60)}秒")

        result = recognize_audio_whisper(file_path)
        if not result:
            return
            
        original_subtitles = [{"start": segment["start"], "end": segment["end"], "text": segment["text"]} 
                            for segment in result["segments"]]

        base_filename = os.path.splitext(os.path.basename(file_path))[0]
        output_dir = os.path.dirname(file_path)
        original_output_file = os.path.join(output_dir, f"{base_filename}.srt")

        generate_srt(original_subtitles, original_output_file)
        print_status(f"原始字幕已保存：{original_output_file}")

        if do_translate:
            first_segment_text = original_subtitles[0]["text"]
            detected_language = detect(first_segment_text)
            if detected_language != "zh-cn":
                translate_srt_file(original_output_file)
            else:
                print_status("字幕已是中文，无需翻译。")

        elapsed_time = time.time() - start_time
        print_status(f"处理完成！总耗时: {elapsed_time:.2f} 秒")
    except Exception as e:
        print_status(f"处理失败: {str(e)}")

def main():
    # 禁用SSL警告
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(description='音视频字幕生成和翻译工具')
    parser.add_argument('file_path', help='音频/视频文件路径或SRT文件路径')
    parser.add_argument('--no-translate', action='store_true', help='不进行翻译，仅生成原始字幕')
    parser.add_argument('--srt', action='store_true', help='输入文件是SRT文件，仅进行翻译')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.file_path):
        print_status(f"错误：文件 {args.file_path} 不存在")
        return
        
    if args.srt:
        if not args.file_path.lower().endswith('.srt'):
            print_status("错误：使用--srt选项时，输入文件必须是SRT格式")
            return
        translate_srt_file(args.file_path)
    else:
        process_audio_file(args.file_path, not args.no_translate)

if __name__ == "__main__":
    main() 
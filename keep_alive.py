from datetime import datetime
from threading import Thread
import sys
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# 用來儲存最近的 Log 紀錄（最多存 100 條）
log_buffer = []

class LogInterceptor:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, message):
        self.original_stream.write(message)
        self.original_stream.flush()
        cleaned = message.strip()
        if cleaned:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_buffer.append(f"[{timestamp}] {cleaned}")
            if len(log_buffer) > 100:
                log_buffer.pop(0)

    def flush(self):
        self.original_stream.flush()

# 將終端機輸出攔截並寫入 buffer
sys.stdout = LogInterceptor(sys.stdout)

@app.route("/")
def home():
    # 這裡會自動去讀取 templates 資料夾底下的 index.html
    return render_template("index.html")

@app.route("/logs_data")
def logs_data():
    return jsonify(logs=log_buffer)

def run():
    app.run(host="0.0.0.0", port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()

from datetime import datetime
from threading import Thread
from flask import Flask, jsonify, render_template_string

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
      # 保持最多 100 筆 Log，避免記憶體爆掉
      if len(log_buffer) > 100:
        log_buffer.pop(0)

  def flush(self):
    self.original_stream.flush()


import sys

sys.stdout = LogInterceptor(sys.stdout)


@app.route("/")
def home():
  # 漂亮的黑色終端機風格網頁
  html_template = """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>Discord Bot 運行日誌</title>
        <style>
            body { background-color: #1e1e1e; color: #d4d4d4; font-family: 'Consolas', 'Courier New', monospace; padding: 20px; }
            h1 { color: #4ec9b0; font-size: 20px; }
            .console { background-color: #0f0f0f; border: 1px solid #333; padding: 15px; border-radius: 5px; height: 400px; overflow-y: scroll; white-space: pre-wrap; font-size: 14px; line-height: 1.5; }
            .status { margin-bottom: 10px; color: #9cdcfe; }
        </style>
        <script>
            // 每 2 秒自動重新整理 Log 畫面
            function fetchLogs() {
                fetch('/logs_data')
                    .then(response => response.json())
                    .then(data => {
                        const consoleDiv = document.getElementById('console');
                        consoleDiv.innerText = data.logs.join('\\n');
                        consoleDiv.scrollTop = consoleDiv.scrollHeight; // 自動滾動到底部
                    });
            }
            setInterval(fetchLogs, 2000);
        </script>
    </head>
    <body>
        <h1>🤖 Discord 機器人即時日誌 (Live Logs)</h1>
        <div class="status">狀態：🟢 運行中 | 每 2 秒自動同步日誌</div>
        <div id="console" class="console">載入中...</div>
    </body>
    </html>
    """
  return render_template_string(html_template)


@app.route("/logs_data")
def logs_data():
  return jsonify(logs=log_buffer)


def run():
  app.run(host="0.0.0.0", port=10000)


def keep_alive():
  t = Thread(target=run)
  t.start()

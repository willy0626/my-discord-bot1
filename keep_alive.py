from datetime import datetime
from threading import Thread
import sys
import asyncio
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# 全域變數，用來讓 web 與 bot 溝通
bot_instance = None
target_voice_channels = {}

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

sys.stdout = LogInterceptor(sys.stdout)

def set_bot(bot, target_dict):
    global bot_instance, target_voice_channels
    bot_instance = bot
    target_voice_channels = target_dict

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/logs_data")
def logs_data():
    return jsonify(logs=log_buffer)

# 取得機器人所在的伺服器與頻道資料
# 確保這裡的路由名稱是 /api_guilds
@app.route("/api_guilds")
def api_guilds():
    if not bot_instance:
        return jsonify([])
    guilds = []
    for guild in bot_instance.guilds:
        channels = [
            {"id": str(c.id), "name": c.name} 
            for c in guild.voice_channels
        ]
        guilds.append({
            "id": str(guild.id),
            "name": guild.name,
            "channels": channels
        })
    return jsonify(guilds)

# 網頁端控制：加入頻道
@app.route("/api/join", methods=["POST"])
def api_join():
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not ready"})
    
    data = request.json
    guild_id = int(data.get("guild_id"))
    channel_id = int(data.get("channel_id"))
    
    guild = bot_instance.get_guild(guild_id)
    if not guild:
        return jsonify({"success": False, "error": "Guild not found"})
        
    channel = guild.get_channel(channel_id)
    if not channel:
        return jsonify({"success": False, "error": "Channel not found"})
        
    target_voice_channels[guild_id] = channel.id
    
    # 透過 asyncio 在背景執行連線動作
    future = asyncio.run_coroutine_threadsafe(connect_channel(guild, channel), bot_instance.loop)
    try:
        future.result(timeout=5)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

async def connect_channel(guild, channel):
    if guild.voice_client:
        await guild.voice_client.move_to(channel)
    else:
        await channel.connect()

# 網頁端控制：離開頻道
@app.route("/api/leave", methods=["POST"])
def api_leave():
    if not bot_instance:
        return jsonify({"success": False, "error": "Bot not ready"})
        
    data = request.json
    guild_id = int(data.get("guild_id"))
    
    guild = bot_instance.get_guild(guild_id)
    if guild and guild.voice_client:
        if guild_id in target_voice_channels:
            del target_voice_channels[guild_id]
        future = asyncio.run_coroutine_threadsafe(guild.voice_client.disconnect(), bot_instance.loop)
        future.result(timeout=5)
        return jsonify({"success": True})
        
    return jsonify({"success": False, "error": "Not in voice channel"})

def run():
    app.run(host="0.0.0.0", port=10000)

def keep_alive(bot, target_dict):
    set_bot(bot, target_dict)
    t = Thread(target=run)
    t.start()

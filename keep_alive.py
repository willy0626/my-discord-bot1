from datetime import datetime, timezone
from threading import Thread, Lock
import sys
import asyncio

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

bot_instance = None
target_voice_channels = {}
connect_callback = None
voice_flag_callback = None
monitor_data = {}
event_history = []
monitor_lock = Lock()

log_buffer = []
log_lock = Lock()

class LogInterceptor:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, message):
        self.original_stream.write(message)
        self.original_stream.flush()
        cleaned = message.strip()
        if cleaned:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            with log_lock:
                log_buffer.append(f"[{timestamp}] {cleaned}")
                del log_buffer[:-300]

    def flush(self):
        self.original_stream.flush()

if not isinstance(sys.stdout, LogInterceptor):
    sys.stdout = LogInterceptor(sys.stdout)

def set_bot(bot, target_dict, connect_func, voice_flag_func, monitor_dict, events_list, data_lock):
    global bot_instance, target_voice_channels, connect_callback
    global voice_flag_callback, monitor_data, event_history, monitor_lock
    bot_instance = bot
    target_voice_channels = target_dict
    connect_callback = connect_func
    voice_flag_callback = voice_flag_func
    monitor_data = monitor_dict
    event_history = events_list
    monitor_lock = data_lock

def run_bot_task(coro, timeout=30):
    if not bot_instance:
        coro.close()
        raise RuntimeError("Bot 尚未初始化")
    loop = bot_instance.loop
    if not loop.is_running():
        coro.close()
        raise RuntimeError("Bot 事件循環尚未啟動")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/logs_data")
def logs_data():
    with log_lock:
        logs = list(log_buffer)
    return jsonify(logs=logs)

@app.route("/api_guilds")
def api_guilds():
    if not bot_instance:
        return jsonify([])
    guilds = []
    for guild in bot_instance.guilds:
        guilds.append({
            "id": str(guild.id),
            "name": guild.name,
            "channels": [{"id": str(c.id), "name": c.name} for c in guild.voice_channels]
        })
    return jsonify(guilds)

@app.route("/api/monitor")
def api_monitor():
    if not bot_instance:
        return jsonify(success=False, error="Bot 尚未初始化"), 503
    guild_id = request.args.get("guild_id")
    with monitor_lock:
        data = dict(monitor_data)
        data["guilds"] = dict(monitor_data.get("guilds", {}))
    if guild_id:
        item = data.get("guilds", {}).get(str(guild_id))
        if item is None:
            return jsonify(success=False, error="找不到該伺服器監控資料"), 404
        data["selected_guild"] = item
    return jsonify(success=True, monitor=data)

@app.route("/api/events")
def api_events():
    guild_id = request.args.get("guild_id")
    with monitor_lock:
        events = list(event_history)
    if guild_id:
        events = [e for e in events if e.get("guild_id") in (None, str(guild_id))]
    return jsonify(success=True, events=events[-100:])

@app.route("/api/join", methods=["POST"])
def api_join():
    if not bot_instance or not connect_callback:
        return jsonify(success=False, error="Bot 尚未準備完成"), 503
    data = request.get_json(silent=True) or {}
    try:
        guild_id, channel_id = int(data.get("guild_id")), int(data.get("channel_id"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="伺服器或頻道 ID 無效"), 400
    guild = bot_instance.get_guild(guild_id)
    if not guild:
        return jsonify(success=False, error="找不到伺服器"), 404
    channel = guild.get_channel(channel_id)
    try:
        import discord
        if not isinstance(channel, discord.VoiceChannel):
            return jsonify(success=False, error="找不到語音頻道"), 404
        target_voice_channels[guild_id] = channel_id
        run_bot_task(connect_callback(channel), timeout=45)
        return jsonify(success=True, message="已加入語音，靜音音訊已啟動")
    except Exception as exc:
        print(f"網站加入語音失敗：{exc}")
        return jsonify(success=False, error=str(exc)), 500

@app.route("/api/leave", methods=["POST"])
def api_leave():
    if not bot_instance:
        return jsonify(success=False, error="Bot 尚未準備完成"), 503
    data = request.get_json(silent=True) or {}
    try:
        guild_id = int(data.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="伺服器 ID 無效"), 400
    guild = bot_instance.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return jsonify(success=False, error="Bot 目前不在語音頻道"), 400

    # 清除目標，避免主動離開後被自動重連
    target_voice_channels.pop(guild_id, None)

    async def disconnect_voice():
        vc = guild.voice_client
        if vc:
            await vc.disconnect(force=True)

    try:
        run_bot_task(disconnect_voice(), timeout=20)
        return jsonify(success=True, message="已離開語音頻道")
    except Exception as exc:
        print(f"網站離開語音失敗：{exc}")
        return jsonify(success=False, error=str(exc)), 500

@app.route("/api/voice-status")
def api_voice_status():
    if not bot_instance:
        return jsonify(success=False, error="Bot 尚未準備完成"), 503
    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="請提供伺服器 ID"), 400
    guild = bot_instance.get_guild(guild_id)
    if not guild:
        return jsonify(success=False, error="找不到伺服器"), 404
    vc = guild.voice_client
    member = guild.me
    state = member.voice if member else None
    return jsonify(
        success=True,
        connected=bool(vc and vc.is_connected()),
        channel=vc.channel.name if vc and vc.channel else None,
        self_deaf=bool(state and state.self_deaf),
        self_mute=bool(state and state.self_mute),
        audio_playing=bool(vc and vc.is_playing())
    )

@app.route("/api/voice-control", methods=["POST"])
def api_voice_control():
    if not bot_instance or not voice_flag_callback:
        return jsonify(success=False, error="Bot 尚未準備完成"), 503
    data = request.get_json(silent=True) or {}
    try:
        guild_id = int(data.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify(success=False, error="伺服器 ID 無效"), 400
    flag, enabled = data.get("flag"), data.get("enabled")
    if flag not in ("deafen", "mute") or not isinstance(enabled, bool):
        return jsonify(success=False, error="控制參數無效"), 400
    guild = bot_instance.get_guild(guild_id)
    if not guild or not guild.voice_client or not guild.voice_client.is_connected():
        return jsonify(success=False, error="Bot 必須先加入語音頻道"), 400
    try:
        result = run_bot_task(voice_flag_callback(guild, flag, enabled), timeout=20)
        return jsonify(success=True, **result, message="語音狀態已更新")
    except Exception as exc:
        print(f"網站語音控制失敗：{exc}")
        return jsonify(success=False, error=str(exc)), 500

def run():
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", "10000")),
            threaded=True, use_reloader=False)

def keep_alive(bot, target_dict, connect_func, voice_flag_func,
               monitor_dict, events_list, data_lock):
    set_bot(bot, target_dict, connect_func, voice_flag_func,
            monitor_dict, events_list, data_lock)
    Thread(target=run, daemon=True, name="FlaskControlPanel").start()
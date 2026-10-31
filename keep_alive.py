from datetime import datetime
from threading import Thread
import sys
import asyncio

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

bot_instance = None
target_voice_channels = {}

connect_callback = None
voice_flag_callback = None

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


def set_bot(bot, target_dict, connect_func, voice_flag_func):
    global bot_instance
    global target_voice_channels
    global connect_callback
    global voice_flag_callback

    bot_instance = bot
    target_voice_channels = target_dict
    connect_callback = connect_func
    voice_flag_callback = voice_flag_func


def run_bot_task(coro, timeout=30):
    """從 Flask 執行緒安全地呼叫 Discord Bot 的 coroutine。"""
    if not bot_instance:
        raise RuntimeError("Bot 尚未初始化")

    loop = bot_instance.loop

    if not loop.is_running():
        raise RuntimeError("Bot 的事件迴圈尚未啟動")

    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/logs_data")
def logs_data():
    return jsonify(logs=log_buffer)


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


@app.route("/api/join", methods=["POST"])
def api_join():
    if not bot_instance or not connect_callback:
        return jsonify({
            "success": False,
            "error": "Bot 尚未準備完成"
        }), 503

    data = request.get_json(silent=True) or {}

    try:
        guild_id = int(data.get("guild_id"))
        channel_id = int(data.get("channel_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "伺服器或頻道 ID 無效"
        }), 400

    guild = bot_instance.get_guild(guild_id)

    if not guild:
        return jsonify({
            "success": False,
            "error": "找不到伺服器"
        }), 404

    channel = guild.get_channel(channel_id)

    if not channel or not hasattr(channel, "connect"):
        return jsonify({
            "success": False,
            "error": "找不到語音頻道"
        }), 404

    try:
        # 使用 bot.py 的共用連線函式
        run_bot_task(connect_callback(channel), timeout=40)

        # 成功連線後才記錄目標頻道
        target_voice_channels[guild_id] = channel.id

        return jsonify({
            "success": True,
            "message": "已加入語音，靜音音訊已啟動"
        })

    except Exception as e:
        print(f"網站加入語音失敗：{e}")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/leave", methods=["POST"])
def api_leave():
    if not bot_instance:
        return jsonify({
            "success": False,
            "error": "Bot 尚未準備完成"
        }), 503

    data = request.get_json(silent=True) or {}

    try:
        guild_id = int(data.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "伺服器 ID 無效"
        }), 400

    guild = bot_instance.get_guild(guild_id)

    if not guild or not guild.voice_client:
        return jsonify({
            "success": False,
            "error": "Bot 目前不在語音頻道"
        }), 400

    try:
        async def disconnect_voice():
            await guild.voice_client.disconnect(force=True)

        run_bot_task(disconnect_voice(), timeout=15)

        target_voice_channels.pop(guild_id, None)

        return jsonify({"success": True})

    except Exception as e:
        print(f"網站離開語音失敗：{e}")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/voice-status")
def api_voice_status():
    if not bot_instance:
        return jsonify({
            "success": False,
            "error": "Bot 尚未準備完成"
        }), 503

    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "請提供伺服器 ID"
        }), 400

    guild = bot_instance.get_guild(guild_id)

    if not guild:
        return jsonify({
            "success": False,
            "error": "找不到伺服器"
        }), 404

    vc = guild.voice_client
    member = guild.me
    voice_state = member.voice if member else None

    return jsonify({
        "success": True,
        "connected": bool(vc and vc.is_connected()),
        "channel": vc.channel.name if vc and vc.channel else None,
        "self_deaf": bool(voice_state and voice_state.self_deaf),
        "self_mute": bool(voice_state and voice_state.self_mute),
        "audio_playing": bool(vc and vc.is_playing())
    })


@app.route("/api/voice-control", methods=["POST"])
def api_voice_control():
    if not bot_instance or not voice_flag_callback:
        return jsonify({
            "success": False,
            "error": "Bot 尚未準備完成"
        }), 503

    data = request.get_json(silent=True) or {}

    try:
        guild_id = int(data.get("guild_id"))
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "伺服器 ID 無效"
        }), 400

    flag = data.get("flag")
    enabled = data.get("enabled")

    if flag not in ("deafen", "mute") or not isinstance(enabled, bool):
        return jsonify({
            "success": False,
            "error": "控制參數無效"
        }), 400

    guild = bot_instance.get_guild(guild_id)

    if not guild or not guild.voice_client or not guild.voice_client.is_connected():
        return jsonify({
            "success": False,
            "error": "Bot 必須先加入語音頻道"
        }), 400

    try:
        result = run_bot_task(
            voice_flag_callback(guild, flag, enabled),
            timeout=15
        )

        return jsonify({
            "success": True,
            "self_deaf": result["self_deaf"],
            "self_mute": result["self_mute"],
            "message": "語音狀態已更新"
        })

    except Exception as e:
        print(f"網站語音控制失敗：{e}")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def run():
    app.run(host="0.0.0.0", port=10000)


def keep_alive(bot, target_dict, connect_func, voice_flag_func):
    set_bot(bot, target_dict, connect_func, voice_flag_func)

    t = Thread(target=run, daemon=True)
    t.start()

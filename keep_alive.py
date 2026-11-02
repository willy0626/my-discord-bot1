from datetime import datetime
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


# ==================================================
# 日誌攔截
# ==================================================

class LogInterceptor:

    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, message):
        self.original_stream.write(message)
        self.original_stream.flush()

        cleaned = message.strip()

        if cleaned:
            timestamp = datetime.now().strftime("%H:%M:%S")

            with log_lock:
                log_buffer.append(
                    f"[{timestamp}] {cleaned}"
                )

                if len(log_buffer) > 200:
                    del log_buffer[:-200]

    def flush(self):
        self.original_stream.flush()


if not isinstance(sys.stdout, LogInterceptor):
    sys.stdout = LogInterceptor(sys.stdout)


# ==================================================
# 初始化
# ==================================================

def set_bot(
    bot,
    target_dict,
    connect_func,
    voice_flag_func,
    monitor_dict,
    events_list,
    data_lock
):
    global bot_instance
    global target_voice_channels
    global connect_callback
    global voice_flag_callback
    global monitor_data
    global event_history
    global monitor_lock

    bot_instance = bot
    target_voice_channels = target_dict
    connect_callback = connect_func
    voice_flag_callback = voice_flag_func
    monitor_data = monitor_dict
    event_history = events_list
    monitor_lock = data_lock


# ==================================================
# 從 Flask 執行 Discord Coroutine
# ==================================================

def run_bot_task(coro, timeout=30):

    if not bot_instance:
        raise RuntimeError("Bot 尚未初始化")

    loop = bot_instance.loop

    if not loop.is_running():
        raise RuntimeError("Bot 事件循環尚未啟動")

    future = asyncio.run_coroutine_threadsafe(
        coro,
        loop
    )

    return future.result(timeout=timeout)


# ==================================================
# 網站
# ==================================================

@app.route("/")
def home():
    return render_template("index.html")


# ==================================================
# 日誌 API
# ==================================================

@app.route("/logs_data")
def logs_data():

    with log_lock:
        logs = list(log_buffer)

    return jsonify(logs=logs)


# ==================================================
# 伺服器清單
# ==================================================

@app.route("/api_guilds")
def api_guilds():

    if not bot_instance:
        return jsonify([])

    guilds = []

    for guild in bot_instance.guilds:

        channels = [
            {
                "id": str(channel.id),
                "name": channel.name
            }
            for channel in guild.voice_channels
        ]

        guilds.append({
            "id": str(guild.id),
            "name": guild.name,
            "channels": channels
        })

    return jsonify(guilds)


# ==================================================
# 最高強度監控 API
# ==================================================

@app.route("/api/monitor")
def api_monitor():

    if not bot_instance:
        return jsonify({
            "success": False,
            "error": "Bot 尚未初始化"
        }), 503

    guild_id = request.args.get("guild_id")

    with monitor_lock:
        data = dict(monitor_data)

    if guild_id:
        guild_data = data.get("guilds", {}).get(str(guild_id))

        if guild_data is None:
            return jsonify({
                "success": False,
                "error": "找不到該伺服器監控資料"
            }), 404

        data["selected_guild"] = guild_data

    return jsonify({
        "success": True,
        "monitor": data
    })


@app.route("/api/events")
def api_events():

    guild_id = request.args.get("guild_id")

    with monitor_lock:
        events = list(event_history)

    if guild_id:
        events = [
            event for event in events
            if event.get("guild_id") in (None, str(guild_id))
        ]

    return jsonify({
        "success": True,
        "events": events[-100:]
    })


# ==================================================
# 加入語音
# ==================================================

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
        run_bot_task(
            connect_callback(channel),
            timeout=40
        )

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


# ==================================================
# 離開語音
# ==================================================

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

        # 先清除目標，避免離開後被自動重新加入
        target_voice_channels.pop(guild_id, None)

        async def disconnect_voice():

            vc = guild.voice_client

            if vc:
                await vc.disconnect(force=True)

        run_bot_task(
            disconnect_voice(),
            timeout=15
        )

        return jsonify({
            "success": True,
            "message": "已離開語音頻道"
        })

    except Exception as e:

        print(f"網站離開語音失敗：{e}")

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ==================================================
# 語音狀態
# ==================================================

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
        "channel": (
            vc.channel.name
            if vc and vc.channel else None
        ),
        "self_deaf": bool(
            voice_state and voice_state.self_deaf
        ),
        "self_mute": bool(
            voice_state and voice_state.self_mute
        ),
        "audio_playing": bool(
            vc and vc.is_playing()
        )
    })


# ==================================================
# 拒聽 / 禁麥
# ==================================================

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

    if (
        not guild
        or not guild.voice_client
        or not guild.voice_client.is_connected()
    ):
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


# ==================================================
# Flask 啟動
# ==================================================

def run():
    app.run(
        host="0.0.0.0",
        port=10000,
        threaded=True,
        use_reloader=False
    )


def keep_alive(
    bot,
    target_dict,
    connect_func,
    voice_flag_func,
    monitor_dict,
    events_list,
    data_lock
):

    set_bot(
        bot,
        target_dict,
        connect_func,
        voice_flag_func,
        monitor_dict,
        events_list,
        data_lock
    )

    thread = Thread(
        target=run,
        daemon=True
    )

    thread.start()

from datetime import datetime, timezone
from threading import Lock
from pathlib import Path
import os
import time
import json
import asyncio
import traceback

import discord
import psutil
from discord.ext import commands, tasks
from keep_alive import keep_alive


# ==================================================
# 基本設定
# ==================================================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

target_voice_channels = {}
voice_locks = {}
voice_flags = {}
audio_sources = {}

start_time = time.monotonic()
boot_time = datetime.now(timezone.utc)

last_voice_disconnect = {}
last_voice_reconnect = {}

# 語音連線統計
voice_sessions = {}
voice_total_disconnects = {}
voice_total_reconnects = {}
voice_longest_seconds = {}

# 監控資料
monitor_data = {}
event_history = []

monitor_lock = Lock()

MAX_EVENTS = 300

# Render Persistent Disk 建議掛載在 /var/data
DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", "/var/data"))

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path("./data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

EVENT_FILE = DATA_DIR / "voice_events.jsonl"
STATS_FILE = DATA_DIR / "voice_stats.json"


# ==================================================
# 工具
# ==================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def get_voice_lock(guild_id):
    if guild_id not in voice_locks:
        voice_locks[guild_id] = asyncio.Lock()

    return voice_locks[guild_id]


def get_voice_flags(guild_id):
    if guild_id not in voice_flags:
        voice_flags[guild_id] = {
            "self_deaf": True,
            "self_mute": False
        }

    return voice_flags[guild_id]


def debug(message):
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    uptime = int(time.monotonic() - start_time)

    print(
        f"[DEBUG][{now}][UPTIME={uptime}s] {message}",
        flush=True
    )


def add_event(guild_id, event_type, message, severity="info"):
    event = {
        "time": iso_now(),
        "guild_id": str(guild_id) if guild_id else None,
        "type": event_type,
        "message": message,
        "severity": severity
    }

    with monitor_lock:
        event_history.append(event)

        if len(event_history) > MAX_EVENTS:
            del event_history[:-MAX_EVENTS]

    try:
        with EVENT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        debug(f"事件紀錄寫入失敗：{e}")

    debug(message)


def save_stats():
    with monitor_lock:
        data = {
            "disconnects": voice_total_disconnects,
            "reconnects": voice_total_reconnects,
            "longest_seconds": voice_longest_seconds
        }

    try:
        with STATS_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        debug(f"統計資料儲存失敗：{e}")


def load_stats():
    if not STATS_FILE.exists():
        return

    try:
        with STATS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        voice_total_disconnects.update(data.get("disconnects", {}))
        voice_total_reconnects.update(data.get("reconnects", {}))
        voice_longest_seconds.update(data.get("longest_seconds", {}))

        debug("已載入歷史語音統計資料")

    except Exception as e:
        debug(f"歷史統計載入失敗：{e}")


def start_voice_session(guild_id, channel):
    current = voice_sessions.get(guild_id)

    if current and current.get("channel_id") == channel.id:
        return

    # 頻道改變代表開始另一段連線
    if current:
        finish_voice_session(guild_id, "語音頻道變更")

    voice_sessions[guild_id] = {
        "started_at": iso_now(),
        "started_monotonic": time.monotonic(),
        "channel_id": channel.id,
        "channel_name": channel.name
    }

    add_event(
        guild_id,
        "voice_connected",
        f"語音連線開始：{channel.name}"
    )


def finish_voice_session(guild_id, reason):
    session = voice_sessions.pop(guild_id, None)

    if not session:
        return

    elapsed = max(
        0,
        time.monotonic() - session["started_monotonic"]
    )

    key = str(guild_id)

    voice_longest_seconds[key] = max(
        float(voice_longest_seconds.get(key, 0)),
        elapsed
    )

    voice_total_disconnects[key] = (
        int(voice_total_disconnects.get(key, 0)) + 1
    )

    save_stats()

    add_event(
        guild_id,
        "voice_disconnected",
        f"語音連線結束：{reason}，本次持續 {elapsed:.1f} 秒",
        "warning"
    )


def get_session_seconds(guild_id):
    session = voice_sessions.get(guild_id)

    if not session:
        return 0

    return max(
        0,
        time.monotonic() - session["started_monotonic"]
    )


# ==================================================
# 靜音音訊來源
# ==================================================

class SilenceSource(discord.AudioSource):

    def __init__(self):
        self.frames_read = 0
        self.last_read = None
        self.lock = Lock()

    def read(self):
        with self.lock:
            self.frames_read += 1
            self.last_read = time.monotonic()

        # 48kHz、雙聲道、16-bit、20ms PCM 靜音
        return b"\x00" * 3840

    def is_opus(self):
        return False

    def cleanup(self):
        pass

    def get_stats(self):
        with self.lock:
            return {
                "frames_read": self.frames_read,
                "last_read": self.last_read
            }


def silence_after(error):
    if error:
        add_event(
            None,
            "audio_error",
            f"靜音播放器錯誤：{error}",
            "error"
        )


# ==================================================
# 語音連線
# ==================================================

async def connect_with_silence(channel):

    guild = channel.guild
    guild_id = guild.id

    async with get_voice_lock(guild_id):

        vc = guild.voice_client

        if vc and vc.is_connected():

            if vc.channel.id != channel.id:
                add_event(
                    guild_id,
                    "voice_move",
                    f"移動頻道：{vc.channel.name} → {channel.name}"
                )

                await vc.move_to(channel)

        else:

            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    debug(f"清理舊語音連線失敗：{e}")

            add_event(
                guild_id,
                "voice_connecting",
                f"正在連接語音頻道：{channel.name}"
            )

            vc = await channel.connect(reconnect=True)

        flags = get_voice_flags(guild_id)

        await guild.change_voice_state(
            channel=channel,
            self_deaf=flags["self_deaf"],
            self_mute=flags["self_mute"]
        )

        if vc.is_paused():
            vc.resume()

        elif not vc.is_playing():

            source = SilenceSource()
            audio_sources[guild_id] = source

            vc.play(
                source,
                after=silence_after
            )

        target_voice_channels[guild_id] = channel.id

        start_voice_session(guild_id, channel)

        last_voice_reconnect[guild_id] = utc_now()

        key = str(guild_id)
        voice_total_reconnects[key] = (
            int(voice_total_reconnects.get(key, 0)) + 1
        )

        save_stats()

        add_event(
            guild_id,
            "voice_ready",
            f"語音連線完成：{channel.name}，靜音音訊播放中"
        )

        return vc


# ==================================================
# 拒聽 / 禁麥
# ==================================================

async def set_voice_flags(guild, flag, enabled):

    async with get_voice_lock(guild.id):

        vc = guild.voice_client

        if not vc or not vc.is_connected():
            raise RuntimeError("Bot 尚未連接語音頻道")

        flags = get_voice_flags(guild.id)

        if flag == "deafen":
            flags["self_deaf"] = bool(enabled)

        elif flag == "mute":
            flags["self_mute"] = bool(enabled)

        else:
            raise ValueError("未知的語音控制項目")

        await guild.change_voice_state(
            channel=vc.channel,
            self_deaf=flags["self_deaf"],
            self_mute=flags["self_mute"]
        )

        add_event(
            guild.id,
            "voice_flags",
            f"語音狀態更新：拒聽={flags['self_deaf']}，禁麥={flags['self_mute']}"
        )

        return dict(flags)


# ==================================================
# 斷線後延遲恢復
# ==================================================

async def delayed_reconnect(guild_id):

    # 給 discord.py 自身的 Voice reconnect 留時間
    await asyncio.sleep(20)

    guild = bot.get_guild(guild_id)

    if not guild:
        return

    target_id = target_voice_channels.get(guild_id)

    if not target_id:
        return

    channel = guild.get_channel(target_id)

    if not isinstance(channel, discord.VoiceChannel):
        return

    vc = guild.voice_client

    # 如果 discord.py 已自行恢復，不再另外連線
    if vc and vc.is_connected():
        add_event(
            guild_id,
            "reconnect_cancelled",
            "Discord 語音連線已自行恢復，取消額外重連"
        )
        return

    try:
        add_event(
            guild_id,
            "reconnect_attempt",
            "確認語音連線失效，開始延遲恢復",
            "warning"
        )

        await connect_with_silence(channel)

    except Exception as e:
        add_event(
            guild_id,
            "reconnect_failed",
            f"自動恢復失敗：{e}",
            "error"
        )
        debug(traceback.format_exc())


# ==================================================
# Discord 事件
# ==================================================

@bot.event
async def on_disconnect():
    add_event(
        None,
        "gateway_disconnect",
        "Discord Gateway 斷線",
        "warning"
    )


@bot.event
async def on_resumed():
    add_event(
        None,
        "gateway_resumed",
        "Discord Gateway Session 已恢復"
    )


@bot.event
async def on_ready():

    add_event(
        None,
        "bot_ready",
        f"Bot 登入成功：{bot.user}，伺服器數量：{len(bot.guilds)}"
    )

    if not update_status.is_running():
        update_status.start()

    if not monitor_loop.is_running():
        monitor_loop.start()

    try:
        synced = await bot.tree.sync()
        debug(f"斜線指令同步成功：{len(synced)} 個")
    except Exception:
        debug(traceback.format_exc())


@bot.event
async def on_voice_state_update(member, before, after):

    if bot.user is None or member.id != bot.user.id:
        return

    guild_id = member.guild.id

    if before.channel and after.channel is None:

        last_voice_disconnect[guild_id] = utc_now()

        finish_voice_session(
            guild_id,
            f"離開頻道：{before.channel.name}"
        )

        add_event(
            guild_id,
            "voice_state_left",
            f"Discord 回報 Bot 已離開：{before.channel.name}",
            "warning"
        )

        # 只排程一次延遲恢復；不在事件中立即搶連線
        task = reconnect_tasks.get(guild_id)

        if task is None or task.done():
            reconnect_tasks[guild_id] = asyncio.create_task(
                delayed_reconnect(guild_id)
            )

    elif before.channel is None and after.channel:

        start_voice_session(guild_id, after.channel)

    elif before.channel and after.channel:

        if before.channel.id != after.channel.id:

            start_voice_session(guild_id, after.channel)

            add_event(
                guild_id,
                "voice_moved",
                f"Discord 回報頻道移動：{before.channel.name} → {after.channel.name}"
            )


@bot.event
async def on_error(event, *args, **kwargs):
    add_event(
        None,
        "event_error",
        f"Discord 事件錯誤：{event}\n{traceback.format_exc()}",
        "error"
    )


reconnect_tasks = {}


# ==================================================
# 狀態切換
# ==================================================

@tasks.loop(seconds=5)
async def update_status():

    if not hasattr(update_status, "toggle"):
        update_status.toggle = 0

    if update_status.toggle == 0:

        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=f"目前在 {len(bot.guilds)} 個伺服器"
        )

    else:

        uptime = int(time.monotonic() - start_time)

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"運行時間：{uptime // 3600} 小時"
        )

    update_status.toggle = 1 - update_status.toggle

    await bot.change_presence(activity=activity)


@update_status.before_loop
async def before_update_status():
    await bot.wait_until_ready()


# ==================================================
# 最高強度監控
# ==================================================

@tasks.loop(seconds=5)
async def monitor_loop():

    loop = asyncio.get_running_loop()
    now = time.monotonic()

    if not hasattr(monitor_loop, "expected"):
        monitor_loop.expected = now + 5

    event_loop_lag = max(
        0,
        (now - monitor_loop.expected) * 1000
    )

    monitor_loop.expected = now + 5

    try:
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory().percent
    except Exception:
        cpu = None
        memory = None

    snapshot = {
        "updated_at": iso_now(),
        "bot_ready": bot.is_ready(),
        "gateway_latency_ms": (
            round(bot.latency * 1000, 2)
            if bot.latency is not None else None
        ),
        "event_loop_lag_ms": round(event_loop_lag, 2),
        "cpu_percent": cpu,
        "memory_percent": memory,
        "process_uptime": int(time.monotonic() - start_time),
        "discord_py_version": discord.__version__,
        "guilds": {}
    }

    for guild in bot.guilds:

        guild_id = guild.id
        key = str(guild_id)

        vc = guild.voice_client
        connected = bool(vc and vc.is_connected())

        voice_latency = None
        voice_average_latency = None

        if vc:
            try:
                value = vc.latency
                if value is not None:
                    voice_latency = round(value * 1000, 2)
            except Exception:
                pass

            try:
                value = vc.average_latency
                if value is not None:
                    voice_average_latency = round(value * 1000, 2)
            except Exception:
                pass

        source = audio_sources.get(guild_id)
        audio_stats = source.get_stats() if source else {
            "frames_read": 0,
            "last_read": None
        }

        last_read = audio_stats["last_read"]

        audio_read_age = (
            round(now - last_read, 2)
            if last_read is not None else None
        )

        session = voice_sessions.get(guild_id)

        voice_state = guild.me.voice if guild.me else None

        snapshot["guilds"][key] = {
            "name": guild.name,
            "connected": connected,
            "channel": (
                vc.channel.name
                if connected and vc.channel else None
            ),
            "voice_latency_ms": voice_latency,
            "voice_average_latency_ms": voice_average_latency,
            "audio_playing": bool(vc and vc.is_playing()),
            "audio_paused": bool(vc and vc.is_paused()),
            "audio_frames_read": audio_stats["frames_read"],
            "audio_last_read_seconds_ago": audio_read_age,
            "session_started_at": (
                session["started_at"] if session else None
            ),
            "session_seconds": round(
                get_session_seconds(guild_id), 1
            ),
            "longest_session_seconds": round(
                float(voice_longest_seconds.get(key, 0)), 1
            ),
            "disconnect_count": int(
                voice_total_disconnects.get(key, 0)
            ),
            "reconnect_count": int(
                voice_total_reconnects.get(key, 0)
            ),
            "last_disconnect": (
                last_voice_disconnect[guild_id].isoformat()
                if guild_id in last_voice_disconnect else None
            ),
            "last_reconnect": (
                last_voice_reconnect[guild_id].isoformat()
                if guild_id in last_voice_reconnect else None
            ),
            "self_deaf": bool(
                voice_state and voice_state.self_deaf
            ),
            "self_mute": bool(
                voice_state and voice_state.self_mute
            ),
            "udp_verified": False,
            "udp_note": "discord.py 公開 API 無法直接確認每個 UDP 封包是否抵達 Discord"
        }

    with monitor_lock:
        monitor_data.clear()
        monitor_data.update(snapshot)

    # 異常預警
    for key, item in snapshot["guilds"].items():

        if item["connected"] and not item["audio_playing"]:
            add_event(
                int(key),
                "audio_not_playing",
                "警告：語音已連線，但靜音播放器沒有播放",
                "warning"
            )

        if (
            item["connected"]
            and item["audio_playing"]
            and item["audio_last_read_seconds_ago"] is not None
            and item["audio_last_read_seconds_ago"] > 3
        ):
            add_event(
                int(key),
                "audio_stalled",
                "警告：靜音音訊超過 3 秒沒有讀取",
                "warning"
            )

        if (
            item["voice_latency_ms"] is not None
            and item["voice_latency_ms"] > 1000
        ):
            add_event(
                int(key),
                "voice_latency_high",
                f"Voice WebSocket 延遲過高：{item['voice_latency_ms']} ms",
                "warning"
            )

    if event_loop_lag > 1000:
        add_event(
            None,
            "event_loop_lag",
            f"事件循環延遲過高：{event_loop_lag:.1f} ms",
            "warning"
        )


@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()


# ==================================================
# 語音頻道選單
# ==================================================

class VoiceSelectDropdown(discord.ui.Select):

    def __init__(self, channels):

        options = [
            discord.SelectOption(
                label=channel.name,
                value=str(channel.id)
            )
            for channel in channels[:25]
        ]

        super().__init__(
            placeholder="請選擇要加入的語音頻道...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):

        if interaction.guild is None:
            await interaction.response.send_message(
                "請在伺服器內使用。",
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(int(self.values[0]))

        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "找不到指定語音頻道。",
                ephemeral=True
            )
            return

        try:
            await connect_with_silence(channel)

            await interaction.response.send_message(
                f"已加入 {channel.name}，靜音音訊正在播放。",
                ephemeral=True
            )

        except Exception as e:
            debug(traceback.format_exc())

            await interaction.response.send_message(
                f"加入失敗：{e}",
                ephemeral=True
            )


class VoiceSelectView(discord.ui.View):

    def __init__(self, channels):
        super().__init__(timeout=180)
        self.add_item(VoiceSelectDropdown(channels))


# ==================================================
# /join
# ==================================================

@bot.tree.command(
    name="join",
    description="加入語音頻道並啟動靜音掛機"
)
async def join(interaction):

    if interaction.guild is None:
        await interaction.response.send_message(
            "請在伺服器內使用。",
            ephemeral=True
        )
        return

    channels = interaction.guild.voice_channels

    if not channels:
        await interaction.response.send_message(
            "這個伺服器沒有語音頻道。",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "請選擇語音頻道：",
        view=VoiceSelectView(channels),
        ephemeral=True
    )


# ==================================================
# /debug
# ==================================================

@bot.tree.command(
    name="debug",
    description="查看 Bot 最高強度監控狀態"
)
async def debug_command(interaction):

    if interaction.guild is None:
        await interaction.response.send_message(
            "請在伺服器內使用。",
            ephemeral=True
        )
        return

    guild = interaction.guild
    vc = guild.voice_client

    connected = bool(vc and vc.is_connected())

    voice_latency = "無資料"
    if vc:
        try:
            voice_latency = f"{vc.latency * 1000:.2f} ms"
        except Exception:
            pass

    session = get_session_seconds(guild.id)

    embed = discord.Embed(
        title="Discord Voice 最高強度監控",
        color=(
            discord.Color.green()
            if connected else discord.Color.red()
        ),
        timestamp=utc_now()
    )

    embed.add_field(
        name="程式運行時間",
        value=f"{(time.monotonic() - start_time) / 3600:.2f} 小時",
        inline=True
    )

    embed.add_field(
        name="本次語音連線",
        value=f"{session / 3600:.2f} 小時",
        inline=True
    )

    embed.add_field(
        name="Voice WS 延遲",
        value=voice_latency,
        inline=True
    )

    embed.add_field(
        name="Gateway 延遲",
        value=f"{bot.latency * 1000:.2f} ms",
        inline=True
    )

    embed.add_field(
        name="語音狀態",
        value="已連線" if connected else "未連線",
        inline=True
    )

    embed.add_field(
        name="靜音音訊",
        value="播放中" if connected and vc.is_playing() else "未播放",
        inline=True
    )

    embed.add_field(
        name="discord.py",
        value=discord.__version__,
        inline=True
    )

    embed.add_field(
        name="UDP 封包確認",
        value="公開 API 無法直接驗證",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ==================================================
# 啟動
# ==================================================

load_stats()

debug(f"discord.py 版本：{discord.__version__}")
debug("正在啟動最高強度監控 Bot...")

keep_alive(
    bot,
    target_voice_channels,
    connect_with_silence,
    set_voice_flags,
    monitor_data,
    event_history,
    monitor_lock
)

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN 環境變數！")

bot.run(TOKEN)

from datetime import datetime, timezone
from threading import Lock
from pathlib import Path
import os
import time
import math
import json
import asyncio
import traceback

import discord
import psutil
from discord.ext import commands, tasks
from keep_alive import keep_alive

# ==============================
# 基本設定
# ==============================
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

target_voice_channels = {}
voice_flags = {}
voice_locks = {}
reconnect_tasks = {}
pending_reconnect = set()

boot_time = time.monotonic()
boot_datetime = datetime.now(timezone.utc)
process = psutil.Process(os.getpid())

monitor_data = {}
event_history = []
monitor_lock = Lock()

voice_sessions = {}
voice_session_monotonic = {}
voice_total_disconnects = {}
voice_total_reconnects = {}
voice_longest_seconds = {}
last_voice_disconnect = {}
last_voice_reconnect = {}

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATS_FILE = DATA_DIR / "voice_stats.json"

def utc_now():
    return datetime.now(timezone.utc)

def iso_now():
    return utc_now().isoformat()

def debug(message):
    print(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}] {message}", flush=True)

def add_event(guild_id, event_type, message, severity="info"):
    item = {
        "time": iso_now(),
        "guild_id": str(guild_id) if guild_id is not None else None,
        "type": str(event_type),
        "message": str(message),
        "severity": severity
    }
    with monitor_lock:
        event_history.append(item)
        del event_history[:-300]
    return item

def load_stats():
    if not STATS_FILE.exists():
        return
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        for key, value in data.items():
            gid = int(key)
            voice_total_disconnects[gid] = int(value.get("disconnects", 0))
            voice_total_reconnects[gid] = int(value.get("reconnects", 0))
            voice_longest_seconds[gid] = float(value.get("longest_seconds", 0))
    except Exception as exc:
        debug(f"讀取統計資料失敗：{exc}")

def save_stats():
    data = {}
    guild_ids = (
        set(voice_total_disconnects)
        | set(voice_total_reconnects)
        | set(voice_longest_seconds)
    )
    for gid in guild_ids:
        data[str(gid)] = {
            "disconnects": voice_total_disconnects.get(gid, 0),
            "reconnects": voice_total_reconnects.get(gid, 0),
            "longest_seconds": voice_longest_seconds.get(gid, 0)
        }
    try:
        temp = STATS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(STATS_FILE)
    except Exception as exc:
        debug(f"儲存統計資料失敗：{exc}")

def get_voice_lock(guild_id):
    if guild_id not in voice_locks:
        voice_locks[guild_id] = asyncio.Lock()
    return voice_locks[guild_id]

def get_voice_flags(guild_id):
    return voice_flags.setdefault(guild_id, {"self_deaf": True, "self_mute": False})

def start_voice_session(guild_id, channel):
    if guild_id in voice_sessions:
        # 頻道移動不視為斷線，不重設連續連線時間
        voice_sessions[guild_id]["channel"] = channel.name
        voice_sessions[guild_id]["channel_id"] = channel.id
        return
    voice_sessions[guild_id] = {
        "started_at": iso_now(),
        "channel": channel.name,
        "channel_id": channel.id
    }
    voice_session_monotonic[guild_id] = time.monotonic()
    add_event(guild_id, "voice_connected", f"已連接語音頻道：{channel.name}")

def finish_voice_session(guild_id, reason):
    started = voice_session_monotonic.pop(guild_id, None)
    voice_sessions.pop(guild_id, None)
    if started is None:
        return
    duration = max(0, time.monotonic() - started)
    voice_total_disconnects[guild_id] = voice_total_disconnects.get(guild_id, 0) + 1
    voice_longest_seconds[guild_id] = max(
        voice_longest_seconds.get(guild_id, 0), duration
    )
    last_voice_disconnect[guild_id] = utc_now()
    save_stats()
    add_event(guild_id, "voice_disconnected",
              f"{reason}；本次連線 {duration:.1f} 秒", "warning")

def get_session_seconds(guild_id):
    started = voice_session_monotonic.get(guild_id)
    return max(0, time.monotonic() - started) if started else 0

# ==============================
# 靜音音訊來源
# ==============================
class SilenceSource(discord.AudioSource):
    def __init__(self):
        self.frames_read = 0
        self.last_read = None
        self._lock = Lock()

    def read(self):
        with self._lock:
            self.frames_read += 1
            self.last_read = time.monotonic()
        # 48 kHz、雙聲道、16-bit、20ms PCM
        return b"\x00" * 3840

    def is_opus(self):
        return False

    def cleanup(self):
        pass

    def snapshot(self):
        with self._lock:
            return self.frames_read, self.last_read

audio_sources = {}

def schedule_audio_recovery(guild_id, error):
    debug(f"靜音播放器錯誤（guild={guild_id}）：{error}")
    add_event(guild_id, "audio_error", str(error), "error")
    loop = bot.loop
    if loop.is_running():
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(recover_audio_player(guild_id))
        )

async def recover_audio_player(guild_id):
    await asyncio.sleep(2)
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client or not guild.voice_client.is_connected():
        return
    vc = guild.voice_client
    async with get_voice_lock(guild_id):
        if guild.voice_client is not vc or not vc.is_connected() or vc.is_playing():
            return
        try:
            source = SilenceSource()
            audio_sources[guild_id] = source
            vc.play(source, after=lambda err: schedule_audio_recovery(guild_id, err) if err else None)
            add_event(guild_id, "audio_recovered", "靜音播放器已重新啟動", "warning")
        except Exception as exc:
            add_event(guild_id, "audio_recovery_failed", str(exc), "error")
            debug(f"播放器恢復失敗：{exc}")

# ==============================
# 語音連線
# ==============================
async def connect_with_silence(channel):
    guild = channel.guild
    guild_id = guild.id

    async with get_voice_lock(guild_id):
        vc = guild.voice_client
        newly_connected = False

        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                old_name = vc.channel.name
                await vc.move_to(channel)
                debug(f"語音頻道移動：{old_name} → {channel.name}")
                if guild_id in voice_sessions:
                    voice_sessions[guild_id]["channel"] = channel.name
                    voice_sessions[guild_id]["channel_id"] = channel.id
        else:
            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception as exc:
                    debug(f"清理舊 VoiceClient 失敗：{exc}")
            vc = await channel.connect(reconnect=True, timeout=30)
            newly_connected = True

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
            vc.play(source, after=lambda err: schedule_audio_recovery(guild_id, err) if err else None)

        if guild_id not in voice_sessions:
            start_voice_session(guild_id, channel)

        if newly_connected and guild_id in pending_reconnect:
            voice_total_reconnects[guild_id] = voice_total_reconnects.get(guild_id, 0) + 1
            pending_reconnect.discard(guild_id)
            save_stats()
            add_event(guild_id, "voice_reconnected", "語音重新連線成功", "warning")
        elif newly_connected:
            add_event(guild_id, "voice_joined", f"首次加入：{channel.name}")

        last_voice_reconnect[guild_id] = utc_now()
        target_voice_channels[guild_id] = channel.id
        debug(f"語音連線就緒：{channel.name} | 拒聽={flags['self_deaf']} | 禁麥={flags['self_mute']} | 音訊播放={vc.is_playing()}")
        return vc

async def set_voice_flags(guild, flag, enabled):
    guild_id = guild.id
    async with get_voice_lock(guild_id):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            raise RuntimeError("Bot 尚未連接語音頻道")

        current = dict(get_voice_flags(guild_id))
        if flag == "deafen":
            current["self_deaf"] = bool(enabled)
        elif flag == "mute":
            current["self_mute"] = bool(enabled)
        else:
            raise ValueError("未知的語音控制項目")

        # API 成功後才更新本機狀態；不停止、不重建音訊播放器
        await guild.change_voice_state(
            channel=vc.channel,
            self_deaf=current["self_deaf"],
            self_mute=current["self_mute"]
        )
        voice_flags[guild_id] = current
        debug(f"語音旗標更新：{guild.name} | deaf={current['self_deaf']} | mute={current['self_mute']} | audio_playing={vc.is_playing()}")
        return current

async def delayed_reconnect(guild_id):
    try:
        await asyncio.sleep(15)  # 先讓 discord.py 內建 Voice reconnect 有機會恢復
        guild = bot.get_guild(guild_id)
        channel_id = target_voice_channels.get(guild_id)
        if not guild or not channel_id:
            return
        vc = guild.voice_client
        if vc and vc.is_connected():
            # discord.py 已自行恢復，不要再建立第二條連線
            if not vc.is_playing():
                await recover_audio_player(guild_id)
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            add_event(guild_id, "reconnect_failed", "找不到原本的語音頻道", "error")
            return

        for attempt in range(1, 4):
            try:
                await connect_with_silence(channel)
                return
            except Exception as exc:
                debug(f"自動重連第 {attempt} 次失敗：{exc}")
                add_event(guild_id, "reconnect_attempt_failed", f"第 {attempt} 次：{exc}", "warning")
                await asyncio.sleep(min(attempt * 10, 30))
    finally:
        reconnect_tasks.pop(guild_id, None)

# ==============================
# 監控
# ==============================
def finite_ms(value):
    try:
        number = float(value) * 1000
        return round(number, 2) if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None

@tasks.loop(seconds=5)
async def monitor_loop():
    now = time.monotonic()
    guilds_snapshot = {}

    try:
        cpu = psutil.cpu_percent()
        memory = psutil.virtual_memory().percent
    except Exception:
        cpu = memory = None

    gateway_ms = finite_ms(bot.latency)
    snapshot = {
        "updated_at": iso_now(),
        "bot_ready": bot.is_ready(),
        "gateway_latency_ms": gateway_ms,
        "event_loop_lag_ms": None,
        "cpu_percent": cpu,
        "memory_percent": memory,
        "process_uptime": round(now - boot_time, 1),
        "discord_py_version": discord.__version__,
        "guild_count": len(bot.guilds),
        "guilds": {}
    }

    loop_start = time.monotonic()
    await asyncio.sleep(0)
    snapshot["event_loop_lag_ms"] = round(max(0, time.monotonic() - loop_start) * 1000, 2)

    for guild in bot.guilds:
        gid = guild.id
        vc = guild.voice_client
        connected = bool(vc and vc.is_connected())
        source = audio_sources.get(gid)
        frames, last_read = source.snapshot() if source else (0, None)
        age = round(max(0, time.monotonic() - last_read), 2) if last_read else None
        voice_state = guild.me.voice if guild.me else None

        voice_latency = finite_ms(vc.latency) if connected else None
        voice_average = finite_ms(vc.average_latency) if connected else None
        session = voice_sessions.get(gid, {})
        guilds_snapshot[str(gid)] = {
            "guild_name": guild.name,
            "connected": connected,
            "channel": vc.channel.name if connected and vc.channel else None,
            "channel_id": str(vc.channel.id) if connected and vc.channel else None,
            "voice_latency_ms": voice_latency,
            "voice_average_latency_ms": voice_average,
            "audio_playing": bool(connected and vc.is_playing()),
            "audio_frames_read": frames,
            "audio_last_read_seconds_ago": age,
            "session_started_at": session.get("started_at"),
            "session_seconds": round(get_session_seconds(gid), 1),
            "longest_session_seconds": round(voice_longest_seconds.get(gid, 0), 1),
            "disconnect_count": voice_total_disconnects.get(gid, 0),
            "reconnect_count": voice_total_reconnects.get(gid, 0),
            "last_voice_disconnect": last_voice_disconnect.get(gid).isoformat() if gid in last_voice_disconnect else None,
            "last_voice_reconnect": last_voice_reconnect.get(gid).isoformat() if gid in last_voice_reconnect else None,
            "self_deaf": bool(voice_state and voice_state.self_deaf),
            "self_mute": bool(voice_state and voice_state.self_mute),
            "udp_verified": False
        }

    snapshot["guilds"] = guilds_snapshot
    with monitor_lock:
        monitor_data.clear()
        monitor_data.update(snapshot)

@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()

@tasks.loop(seconds=5)
async def update_status():
    if not hasattr(update_status, "toggle"):
        update_status.toggle = False
    if update_status.toggle:
        uptime = int(time.monotonic() - boot_time)
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"運行 {uptime // 86400}天 {(uptime % 86400) // 3600}時"
        )
    else:
        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=f"目前在 {len(bot.guilds)} 個伺服器"
        )
    update_status.toggle = not update_status.toggle
    await bot.change_presence(activity=activity)

@update_status.before_loop
async def before_update_status():
    await bot.wait_until_ready()

# ==============================
# Discord 事件
# ==============================
@bot.event
async def on_disconnect():
    debug("Discord Gateway 已中斷；這不代表 Voice 一定斷線")
    add_event(None, "gateway_disconnected", "Discord Gateway 已中斷", "warning")

@bot.event
async def on_resumed():
    debug("Discord Gateway Session 已恢復")
    add_event(None, "gateway_resumed", "Discord Gateway Session 已恢復")

@bot.event
async def on_ready():
    debug(f"登入成功：{bot.user} | guilds={len(bot.guilds)}")
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
    gid = member.guild.id

    if before.channel is not None and after.channel is None:
        # 如果沒有預設目標，視為使用者/管理員主動移除
        if gid not in target_voice_channels:
            finish_voice_session(gid, "Bot 離開語音頻道")
            return

        finish_voice_session(gid, "Discord Voice 狀態回報斷線")
        pending_reconnect.add(gid)
        last_voice_disconnect[gid] = utc_now()
        add_event(gid, "voice_disconnect_detected", "偵測到語音離線，等待內建重連及恢復檢查", "warning")

        old_task = reconnect_tasks.get(gid)
        if old_task and not old_task.done():
            old_task.cancel()
        reconnect_tasks[gid] = asyncio.create_task(delayed_reconnect(gid))

    elif before.channel is None and after.channel is not None:
        # 若是剛加入，建立 session；若是自動重連，也會由 connect 函式處理
        if gid not in voice_sessions:
            start_voice_session(gid, after.channel)
        debug(f"Bot 已加入語音：{member.guild.name} / {after.channel.name}")

    elif before.channel and after.channel and before.channel.id != after.channel.id:
        # 頻道移動不增加斷線統計、不重設 session 起點
        if gid in voice_sessions:
            voice_sessions[gid]["channel"] = after.channel.name
            voice_sessions[gid]["channel_id"] = after.channel.id
        add_event(gid, "voice_channel_moved", f"{before.channel.name} → {after.channel.name}")

@bot.event
async def on_error(event, *args, **kwargs):
    debug(f"事件錯誤：{event}\n{traceback.format_exc()}")
    add_event(None, "discord_event_error", event, "error")

# ==============================
# 指令
# ==============================
class VoiceSelectDropdown(discord.ui.Select):
    def __init__(self, channels):
        options = [
            discord.SelectOption(label=channel.name[:100], value=str(channel.id))
            for channel in channels[:25]
        ]
        super().__init__(placeholder="請選擇要加入的語音頻道...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(self.values[0]))
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("找不到指定語音頻道。", ephemeral=True)
            return
        target_voice_channels[interaction.guild.id] = channel.id
        try:
            await connect_with_silence(channel)
            flags = get_voice_flags(interaction.guild.id)
            await interaction.response.send_message(
                f"已加入 **{channel.name}**\n拒聽：{'開啟' if flags['self_deaf'] else '關閉'}\n禁麥：{'開啟' if flags['self_mute'] else '關閉'}\n靜音播放器：播放中",
                ephemeral=True
            )
        except Exception as exc:
            debug(f"加入語音失敗：{exc}\n{traceback.format_exc()}")
            await interaction.response.send_message(f"加入失敗：{exc}", ephemeral=True)

class VoiceSelectView(discord.ui.View):
    def __init__(self, channels):
        super().__init__(timeout=180)
        self.add_item(VoiceSelectDropdown(channels))

@bot.tree.command(name="join", description="選擇語音頻道並加入")
async def join(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
        return
    channels = interaction.guild.voice_channels
    if not channels:
        await interaction.response.send_message("這個伺服器沒有語音頻道。", ephemeral=True)
        return
    await interaction.response.send_message("請選擇語音頻道：", view=VoiceSelectView(channels), ephemeral=True)

@bot.tree.command(name="debug", description="查看 Bot 運行及語音連線狀態")
async def debug_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
        return
    guild = interaction.guild
    vc = guild.voice_client
    connected = bool(vc and vc.is_connected())
    voice_state = guild.me.voice if guild.me else None
    uptime = int(time.monotonic() - boot_time)
    embed = discord.Embed(
        title="Discord Bot 偵錯面板",
        color=discord.Color.green() if connected else discord.Color.red(),
        timestamp=utc_now()
    )
    embed.add_field(name="程式運行時間", value=f"{uptime // 86400}天 {(uptime % 86400) // 3600}小時 {(uptime % 3600) // 60}分", inline=False)
    embed.add_field(name="Gateway Ping", value=f"{bot.latency * 1000:.2f} ms" if math.isfinite(bot.latency) else "無資料")
    embed.add_field(name="語音連線", value="已連線" if connected else "未連線")
    embed.add_field(name="目前頻道", value=vc.channel.name if connected else "無", inline=False)
    embed.add_field(name="靜音音訊", value="播放中" if connected and vc.is_playing() else "未播放")
    embed.add_field(name="自我拒聽", value="開啟" if voice_state and voice_state.self_deaf else "關閉")
    embed.add_field(name="自我禁麥", value="開啟" if voice_state and voice_state.self_mute else "關閉")
    embed.add_field(name="本次連線時間", value=f"{get_session_seconds(guild.id):.0f} 秒", inline=False)
    embed.add_field(name="斷線 / 重連", value=f"{voice_total_disconnects.get(guild.id, 0)} / {voice_total_reconnects.get(guild.id, 0)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==============================
# 啟動
# ==============================
load_stats()
debug(f"discord.py 版本：{discord.__version__}")
debug("正在啟動 Discord Bot...")
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
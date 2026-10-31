
from datetime import datetime
import os
import time
import traceback
import discord
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

start_time = datetime.utcnow()
boot_time = time.monotonic()

last_voice_disconnect = {}
last_voice_reconnect = {}


# ==============================
# 偵錯工具
# ==============================

def debug(message):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    uptime = int(time.monotonic() - boot_time)

    print(
        f"[DEBUG][{now}][UPTIME={uptime}s] {message}",
        flush=True
    )


# ==============================
# 靜音音訊
# ==============================

class SilenceSource(discord.AudioSource):

    def read(self):
        # 48kHz、雙聲道、16-bit、20ms PCM 靜音
        return b"\x00" * 3840


async def connect_with_silence(channel):

    guild = channel.guild
    vc = guild.voice_client

    if vc and vc.is_connected():

        if vc.channel.id != channel.id:
            await vc.move_to(channel)

        # 確保自我拒聽開啟
        await guild.change_voice_state(
            channel=channel,
            self_deaf=True
        )

    else:

        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                debug(f"清理舊語音連線失敗：{e}")

        vc = await channel.connect(
            reconnect=True,
            self_deaf=True
        )

    # 啟動靜音音訊
    if not vc.is_playing():
        vc.play(SilenceSource())

    last_voice_reconnect[guild.id] = datetime.utcnow()

    debug(
        f"✅ 語音連線成功：{channel.name} "
        "| 靜音音訊已啟動 | 自我拒聽已開啟"
    )

    return vc


# ==============================
# Bot 狀態切換
# ==============================

@tasks.loop(seconds=5)
async def update_status():

    if not hasattr(update_status, "state_toggle"):
        update_status.state_toggle = 0

    if update_status.state_toggle == 0:

        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=f"目前在 {len(bot.guilds)} 個伺服器"
        )

        update_status.state_toggle = 1

    else:

        uptime = datetime.utcnow() - start_time

        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"運行時間：{days}天 {hours}時 {minutes}分"
        )

        update_status.state_toggle = 0

    await bot.change_presence(activity=activity)


# ==============================
# Discord Gateway 偵錯
# ==============================

@bot.event
async def on_disconnect():
    debug("⚠️ Discord Gateway 斷線")


@bot.event
async def on_resumed():
    debug("✅ Discord Gateway 成功恢復 Session")


@bot.event
async def on_ready():

    debug(f"登入成功：{bot.user}")
    debug(f"伺服器數量：{len(bot.guilds)}")

    if not update_status.is_running():
        update_status.start()

    try:
        synced = await bot.tree.sync()
        debug(f"斜線指令同步成功：{len(synced)} 個")

    except Exception:
        debug(traceback.format_exc())


# ==============================
# 語音連線監控及自動重連
# ==============================

@bot.event
async def on_voice_state_update(member, before, after):

    if bot.user is None or member.id != bot.user.id:
        return

    guild_id = member.guild.id

    # Bot 離開語音頻道
    if before.channel and after.channel is None:

        last_voice_disconnect[guild_id] = datetime.utcnow()

        debug(
            f"⚠️ 語音斷線：{member.guild.name} / "
            f"{before.channel.name}"
        )

        if guild_id in target_voice_channels:

            channel = member.guild.get_channel(
                target_voice_channels[guild_id]
            )

            if channel:

                try:
                    debug(f"準備自動重連：{channel.name}")

                    await connect_with_silence(channel)

                except Exception as e:

                    debug(f"❌ 自動重連失敗：{e}")
                    debug(traceback.format_exc())

    # Bot 加入語音頻道
    elif before.channel is None and after.channel:

        debug(
            f"✅ 已加入語音頻道：{member.guild.name} / "
            f"{after.channel.name}"
        )

    # Bot 移動語音頻道
    elif before.channel and after.channel:

        if before.channel.id != after.channel.id:

            debug(
                f"🔄 頻道移動：{before.channel.name} "
                f"→ {after.channel.name}"
            )


# ==============================
# Discord 錯誤紀錄
# ==============================

@bot.event
async def on_error(event, *args, **kwargs):

    debug(f"❌ 事件錯誤：{event}")
    debug(traceback.format_exc())


# ==============================
# 語音頻道選單
# ==============================

class VoiceSelectDropdown(discord.ui.Select):

    def __init__(self, channels):

        options = [
            discord.SelectOption(
                label=channel.name,
                value=str(channel.id)
            )
            for channel in channels
        ]

        super().__init__(
            placeholder="請選擇要加入的語音頻道...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        channel_id = int(self.values[0])
        channel = interaction.guild.get_channel(channel_id)
        guild_id = interaction.guild.id

        if channel and isinstance(channel, discord.VoiceChannel):

            target_voice_channels[guild_id] = channel.id

            try:

                await connect_with_silence(channel)

                await interaction.response.send_message(
                    f"✅ 已加入 **{channel.name}**\n"
                    "🔇 自我拒聽：已開啟\n"
                    "🔇 靜音音訊：正在傳送",
                    ephemeral=True
                )

            except Exception as e:

                debug(f"加入語音頻道失敗：{e}")
                debug(traceback.format_exc())

                await interaction.response.send_message(
                    f"加入頻道失敗：{e}",
                    ephemeral=True
                )

        else:

            await interaction.response.send_message(
                "找不到指定的語音頻道！",
                ephemeral=True
            )


class VoiceSelectView(discord.ui.View):

    def __init__(self, channels):

        super().__init__(timeout=180)
        self.add_item(VoiceSelectDropdown(channels))


# ==============================
# /join
# ==============================

@bot.tree.command(
    name="join",
    description="加入語音頻道並啟動靜音掛機"
)
async def join(interaction: discord.Interaction):

    if interaction.guild is None:
        await interaction.response.send_message(
            "請在伺服器內使用此指令。",
            ephemeral=True
        )
        return

    voice_channels = interaction.guild.voice_channels

    if not voice_channels:

        await interaction.response.send_message(
            "這個伺服器沒有語音頻道！",
            ephemeral=True
        )
        return

    view = VoiceSelectView(voice_channels)

    await interaction.response.send_message(
        "請選擇要加入的語音頻道：",
        view=view,
        ephemeral=True
    )


# ==============================
# /debug
# ==============================

@bot.tree.command(
    name="debug",
    description="查看 Bot 運行及語音連線狀態"
)
async def debug_command(interaction: discord.Interaction):

    if interaction.guild is None:

        await interaction.response.send_message(
            "請在伺服器內使用。",
            ephemeral=True
        )
        return

    uptime = int(time.monotonic() - boot_time)

    days = uptime // 86400
    hours = (uptime % 86400) // 3600
    minutes = (uptime % 3600) // 60
    seconds = uptime % 60

    vc = interaction.guild.voice_client

    connected = bool(vc and vc.is_connected())
    playing = bool(connected and vc.is_playing())

    voice_status = "🟢 已連線" if connected else "🔴 未連線"
    audio_status = "🟢 正在傳送" if playing else "🔴 未傳送"

    voice_channel = vc.channel.name if connected else "無"

    voice_state = interaction.guild.me.voice
    deaf_status = (
        "🟢 已拒聽"
        if voice_state and voice_state.self_deaf
        else "🔴 未拒聽"
    )

    guild_id = interaction.guild.id

    disconnect_time = last_voice_disconnect.get(guild_id)
    reconnect_time = last_voice_reconnect.get(guild_id)

    disconnect_text = (
        disconnect_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if disconnect_time else "尚無紀錄"
    )

    reconnect_text = (
        reconnect_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if reconnect_time else "尚無紀錄"
    )

    embed = discord.Embed(
        title="🤖 Discord Bot 偵錯面板",
        color=(
            discord.Color.green()
            if connected else discord.Color.red()
        ),
        timestamp=datetime.utcnow()
    )

    embed.add_field(
        name="⏱️ 程式運行時間",
        value=f"{days}天 {hours}小時 {minutes}分 {seconds}秒",
        inline=False
    )

    embed.add_field(
        name="📡 Gateway Ping",
        value=f"{bot.latency * 1000:.2f} ms",
        inline=True
    )

    embed.add_field(
        name="🌐 伺服器數量",
        value=str(len(bot.guilds)),
        inline=True
    )

    embed.add_field(
        name="🎧 語音連線",
        value=voice_status,
        inline=True
    )

    embed.add_field(
        name="📍 目前語音頻道",
        value=voice_channel,
        inline=False
    )

    embed.add_field(
        name="🔇 靜音音訊",
        value=audio_status,
        inline=True
    )

    embed.add_field(
        name="🙉 自我拒聽",
        value=deaf_status,
        inline=True
    )

    embed.add_field(
        name="⚠️ 上次語音斷線",
        value=disconnect_text,
        inline=False
    )

    embed.add_field(
        name="🔄 上次成功重連",
        value=reconnect_text,
        inline=False
    )

    embed.set_footer(text="Bot Debug Monitor")

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ==============================
# 啟動
# ==============================

debug("正在啟動 Discord Bot...")

keep_alive(bot, target_voice_channels)

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("找不到 DISCORD_TOKEN 環境變數！")

bot.run(TOKEN)

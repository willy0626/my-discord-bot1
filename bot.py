
from datetime import datetime, timedelta
import os
import time
import traceback
import discord
from discord import app_commands
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

# 啟動時間
start_time = datetime.utcnow()
boot_time = time.monotonic()

# 語音連線紀錄
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
# Bot 狀態切換
# ==============================

@tasks.loop(seconds=5)
async def update_status():

    if not hasattr(update_status, "state_toggle"):
        update_status.state_toggle = 0

    if update_status.state_toggle == 0:

        guild_count = len(bot.guilds)

        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=f"目前在 {guild_count} 個伺服器"
        )

        update_status.state_toggle = 1

    else:

        now = datetime.utcnow()
        uptime = now - start_time

        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        uptime_str = f"運行時間：{days}天 {hours}時 {minutes}分"

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=uptime_str
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

    debug(f"登入成功！身份：{bot.user}")
    debug(f"目前伺服器數量：{len(bot.guilds)}")

    if not update_status.is_running():
        update_status.start()

    try:
        synced = await bot.tree.sync()
        debug(f"斜線指令同步成功：{len(synced)} 個")

    except Exception:
        debug("斜線指令同步失敗：")
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
            f"⚠️ 語音斷線：伺服器={member.guild.name} "
            f"頻道={before.channel.name}"
        )

        if guild_id in target_voice_channels:

            channel_id = target_voice_channels[guild_id]
            channel = member.guild.get_channel(channel_id)

            if channel:

                try:
                    debug(f"準備自動重連：{channel.name}")

                    await channel.connect()

                    last_voice_reconnect[guild_id] = datetime.utcnow()

                    debug(f"✅ 自動重連成功：{channel.name}")

                except Exception as e:

                    debug(f"❌ 自動重連失敗：{e}")
                    debug(traceback.format_exc())

    # Bot 成功加入語音頻道
    elif before.channel is None and after.channel:

        last_voice_reconnect[guild_id] = datetime.utcnow()

        debug(
            f"✅ 已加入語音頻道：{member.guild.name} / "
            f"{after.channel.name}"
        )

    # Bot 移動語音頻道
    elif before.channel and after.channel:

        if before.channel.id != after.channel.id:

            debug(
                f"🔄 語音頻道移動："
                f"{before.channel.name} → {after.channel.name}"
            )


# ==============================
# Discord 錯誤紀錄
# ==============================

@bot.event
async def on_error(event, *args, **kwargs):

    debug(f"❌ 事件發生錯誤：{event}")
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
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):

        channel_id = int(self.values[0])
        channel = interaction.guild.get_channel(channel_id)
        guild_id = interaction.guild.id

        if channel and isinstance(channel, discord.VoiceChannel):

            target_voice_channels[guild_id] = channel.id

            if interaction.guild.voice_client:

                await interaction.guild.voice_client.move_to(channel)

                await interaction.response.send_message(
                    f"成功移動並鎖定語音頻道：**{channel.name}**",
                    ephemeral=True,
                )

            else:

                try:
                    await channel.connect()

                    await interaction.response.send_message(
                        f"成功加入語音頻道：**{channel.name}**",
                        ephemeral=True,
                    )

                except Exception as e:

                    debug(f"手動加入語音頻道失敗：{e}")

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
# /join 指令
# ==============================

@bot.tree.command(
    name="join",
    description="選擇語音頻道讓機器人加入"
)
async def join(interaction: discord.Interaction):

    voice_channels = interaction.guild.voice_channels

    if not voice_channels:

        await interaction.response.send_message(
            "這個伺服器內沒有找到任何語音頻道！",
            ephemeral=True
        )

        return

    view = VoiceSelectView(voice_channels)

    await interaction.response.send_message(
        "請從下方選單選擇你要機器人進去的語音頻道：",
        view=view,
        ephemeral=True
    )


# ==============================
# /debug 偵錯指令
# ==============================

@bot.tree.command(
    name="debug",
    description="查看機器人運行及語音連線診斷資訊"
)
async def debug_command(interaction: discord.Interaction):

    if interaction.guild is None:

        await interaction.response.send_message(
            "請在 Discord 伺服器內使用此指令。",
            ephemeral=True
        )

        return

    now = datetime.utcnow()

    # 程式運行時間
    uptime = int(time.monotonic() - boot_time)

    days = uptime // 86400
    hours = (uptime % 86400) // 3600
    minutes = (uptime % 3600) // 60
    seconds = uptime % 60

    uptime_text = (
        f"{days}天 {hours}小時 "
        f"{minutes}分 {seconds}秒"
    )

    # Gateway Ping
    latency = round(bot.latency * 1000, 2)

    # 語音連線
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():

        voice_status = "🟢 已連線"
        voice_channel = vc.channel.name

    else:

        voice_status = "🔴 未連線"
        voice_channel = "無"

    guild_id = interaction.guild.id

    # 上次斷線
    disconnect_time = last_voice_disconnect.get(guild_id)

    disconnect_text = (
        disconnect_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if disconnect_time else "尚無紀錄"
    )

    # 上次成功加入／重連
    reconnect_time = last_voice_reconnect.get(guild_id)

    reconnect_text = (
        reconnect_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        if reconnect_time else "尚無紀錄"
    )

    embed = discord.Embed(
        title="🤖 Discord Bot 偵錯面板",
        color=(
            discord.Color.green()
            if vc and vc.is_connected()
            else discord.Color.red()
        ),
        timestamp=now
    )

    embed.add_field(
        name="⏱️ 程式運行時間",
        value=uptime_text,
        inline=False
    )

    embed.add_field(
        name="📡 Gateway Ping",
        value=f"{latency} ms",
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
        name="⚠️ 上次語音斷線",
        value=disconnect_text,
        inline=False
    )

    embed.add_field(
        name="🔄 上次成功加入／重連",
        value=reconnect_text,
        inline=False
    )

    embed.set_footer(
        text="Bot Debug Monitor"
    )

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

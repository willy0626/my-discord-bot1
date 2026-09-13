from datetime import datetime, timedelta
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

target_voice_channels = {}

# 記錄機器人啟動的時間點，用來計算運行時間
start_time = datetime.utcnow()


# 定義一個背景任務，每 5 秒切換一次狀態
@tasks.loop(seconds=5)
async def update_status():
  # 使用一個狀態切換開關（利用迴圈輪流顯示不同內容）
  if not hasattr(update_status, "state_toggle"):
    update_status.state_toggle = 0

  if update_status.state_toggle == 0:
    # 狀態 1：正在遊玩 - 目前在 X 個伺服器
    guild_count = len(bot.guilds)
    activity = discord.Activity(
        type=discord.ActivityType.playing, name=f"目前在 {guild_count} 個伺服器"
    )
    update_status.state_toggle = 1
  else:
    # 狀態 2：正在觀看 - 運行時間計算
    now = datetime.utcnow()
    uptime = now - start_time

    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    uptime_str = f"運行時間：{days}天 {hours}時 {minutes}分"
    activity = discord.Activity(
        type=discord.ActivityType.watching, name=uptime_str
    )
    update_status.state_toggle = 0

  await bot.change_presence(activity=activity)


@bot.event
async def on_ready():
  print(f"登入成功！目前身份：{bot.user}")

  # 啟動 5 秒切換狀態的循環任務
  if not update_status.is_running():
    update_status.start()

  try:
    synced = await bot.tree.sync()
    print(f"已同步 {len(synced)} 個斜線指令")
  except Exception as e:
    print(f"同步指令失敗: {e}")


@bot.event
async def on_voice_state_update(member, before, after):
  if member.id == bot.user.id:
    guild_id = member.guild.id
    if before.channel and after.channel is None:
      print(f"[語音斷線警報] 機器人從 {before.channel.name} 斷線了！")
      if guild_id in target_voice_channels:
        channel_id = target_voice_channels[guild_id]
        channel = member.guild.get_channel(channel_id)
        if channel:
          try:
            await channel.connect()
            print(f"[自動重連成功] 已重新回到語音頻道：{channel.name}")
          except Exception as e:
            print(f"[重連失敗] 錯誤原因: {e}")


class VoiceSelectDropdown(discord.ui.Select):

  def __init__(self, channels):
    options = [
        discord.SelectOption(label=channel.name, value=str(channel.id))
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
            f"成功移動並鎖定語音頻道：**{channel.name}**（具備自動重連保護）",
            ephemeral=True,
        )
      else:
        try:
          await channel.connect()
          await interaction.response.send_message(
              f"成功加入語音頻道：**{channel.name}**，開始 1000 小時持久掛機！",
              ephemeral=True,
          )
        except Exception as e:
          await interaction.response.send_message(
              f"加入頻道失敗: {e}", ephemeral=True
          )
    else:
      await interaction.response.send_message(
          "找不到指定的語音頻道！", ephemeral=True
      )


class VoiceSelectView(discord.ui.View):

  def __init__(self, channels):
    super().__init__()
    self.add_item(VoiceSelectDropdown(channels))


@bot.tree.command(
    name="join", description="選擇一個語音頻道讓機器人加入並開啟持久掛機"
)
async def join(interaction: discord.Interaction):
  voice_channels = interaction.guild.voice_channels

  if not voice_channels:
    await interaction.response.send_message(
        "這個伺服器內沒有找到任何語音頻道！", ephemeral=True
    )
    return

  view = VoiceSelectView(voice_channels)
  await interaction.response.send_message(
      "請從下方選單選擇你要機器人進去的語音頻道：", view=view, ephemeral=True
  )


keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
